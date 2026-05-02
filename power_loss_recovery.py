# Support for power loss recovery on klipper based 3d printers
#
# Copyright (C) 2026  Michal Bitala <yzeroy14@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import json
import os
from collections import deque
from typing import Dict, Any, Optional, Tuple, Deque

def load_config(config):
	return PowerLossRecovery(config)

class PowerLossRecovery:
	
	def _parse_gcode_config_option(self, config, option_name, default=''):
		try:
			raw_value = config.get(option_name, default)
			normalized_value = raw_value.replace(',', '\n')
			lines = [line.strip() for line in normalized_value.split('\n') if line.strip()]
			
			if self.debug_mode:
				logging.info(f"PowerLossRecovery: Parsed {option_name}: {len(lines)} lines/commands")
				
			return raw_value, lines
			
		except Exception as e:
			raise config.error(f"Error parsing {option_name}: {str(e)}")
			
	def __init__(self, config):
		self.printer = config.get_printer()
		self.reactor = self.printer.get_reactor()
		
		try:
			self.save_interval = config.getfloat('save_interval', 30., minval=0., maxval=300.)
			self.save_on_layer = config.getboolean('save_on_layer', True)
			self.variables_file = config.get('variables_file', '~/printer_state_vars.cfg')
			self.debug_mode = config.getboolean('debug_mode', False)
			self.resuming_print = False
			
			self.part_cooling_fans = []
			fans_str = config.get('part_cooling_fans', '')
			if fans_str:
				self.part_cooling_fans = [fan.strip() for fan in fans_str.split(',') if fan.strip()]
			
			self.history_size = config.getint('history_size', 5, minval=2, maxval=20)
			self.save_delay = config.getint('save_delay', 2, minval=0, maxval=self.history_size - 1)
			
			try:
				gcode_configs = {
					'restart_gcode': ('restart_gcode', 'restart_gcode_lines'),
					'before_resume_gcode': ('before_resume_gcode', 'before_resume_gcode_lines'),
					'after_resume_gcode': ('after_resume_gcode', 'after_resume_gcode_lines')
				}
				
				for config_name, (raw_attr, lines_attr) in gcode_configs.items():
					raw_value, lines = self._parse_gcode_config_option(config, config_name)
					setattr(self, raw_attr, raw_value)
					setattr(self, lines_attr, lines)
						
			except Exception as e:
				raise config.error(f"Error parsing G-code configurations: {str(e)}")

			self.time_based_enabled = self.save_interval > 0
			self.current_z_height = 0.
				
		except Exception as e:
			raise config.error(f"Error reading PowerLossRecovery config: {str(e)}")
		
		self.gcode = self.printer.lookup_object('gcode')
		self.save_variables = None
		self.toolhead = None
		self.extruder = None
		self.heater_bed = None
		self.last_layer = 0
		self.is_active = False
		self.last_save_time = 0
		self._last_layer_change_time = 0
		self._last_extruder_change_time = 0
		self._consecutive_failures = 0
		self._last_save_attempt = 0
		self.power_loss_recovery_enabled = False
		
		self.state_history: Deque[Dict[str, Any]] = deque(maxlen=self.history_size)
		
		self.name = config.get_name()
				
		try:
			self.save_variables = self.printer.load_object(config, 'save_variables')
		except self.printer.config_error as e:
			raise self.printer.config_error("save_variables module required for PLR storage")
										  
		self.printer.register_event_handler("klippy:connect", self._handle_connect)
		self.printer.register_event_handler("klippy:ready", self._handle_ready)
		self.printer.register_event_handler("extruder:activate_extruder", self._handle_activate_extruder)
										  
		self.gcode.register_command('PLR_SAVE_PRINT_STATE', self.cmd_PLR_SAVE_PRINT_STATE,
								  desc=self.cmd_PLR_SAVE_PRINT_STATE_help)
		
		if self.save_on_layer:
			self.gcode.register_command('PLR_SAVE_PRINT_STATE_WITH_LAYER',
								  self._handle_layer_change,
								  desc="Layer change handler for PowerLossRecovery")
	
		self.gcode.register_command('PLR_QUERY_SAVED_STATE', self.cmd_PLR_QUERY_SAVED_STATE,
								  desc=self.cmd_PLR_QUERY_SAVED_STATE_help)
		
		self.gcode.register_command('PLR_RESET_PRINT_DATA', self.cmd_PLR_RESET_PRINT_DATA,
								  desc=self.cmd_PLR_RESET_PRINT_DATA_help)

		self.gcode.register_command('PLR_ENABLE', self.cmd_PLR_ENABLE,
								  desc=self.cmd_PLR_ENABLE_help)
								  
		self.gcode.register_command('PLR_DISABLE', self.cmd_PLR_DISABLE,
								  desc=self.cmd_PLR_DISABLE_help)
								  
		self.gcode.register_command('PLR_RESUME_PRINT', self.cmd_PLR_RESUME_PRINT,
								desc=self.cmd_PLR_RESUME_PRINT_help)

	def _debug_log(self, message):
		if not self.debug_mode:
			return
		prefix = "PLR LOG:: "
		formatted_msg = f"{prefix}{message}"
		self.gcode.respond_info(formatted_msg)
	
	def _restore_fan_speeds(self, state_data):
		try:
			if not state_data or 'fan_speeds' not in state_data:
				return
		
			fan_speeds = state_data.get('fan_speeds', {})
			if not fan_speeds:
				return
		
			toolhead = self.printer.lookup_object('toolhead')
			cur_extruder = toolhead.get_extruder().get_name()
			
			for fan_name in self.part_cooling_fans:
				try:
					if fan_name not in fan_speeds:
						continue
						
					speed = fan_speeds[fan_name]
					
					if fan_name == 'fan' or fan_name == f"{cur_extruder}_fan":
						speed_byte = int(speed * 255. + .5)
						fan_cmd = f"M106 P0 S{speed_byte}"
					else:
						fan = self.printer.lookup_object(fan_name, None)
						if fan is None:
							continue
						fan_cmd = f"SET_FAN_SPEED FAN={fan_name} SPEED={speed}"
					
					self.gcode.run_script_from_command(fan_cmd)
					
				except Exception as e:
					if self.debug_mode:
						self._debug_log(f"Error restoring {fan_name} speed: {str(e)}")
		
		except Exception as e:
			if self.debug_mode:
				self._debug_log(f"Error in fan speed restoration: {str(e)}")

	def _verify_state(self, state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
		if not isinstance(state, dict):
			return False, "State must be a dictionary"
			
		required_fields = {
			'position': {
				'type': dict,
				'subfields': {'x': (float, int), 'y': (float, int), 'z': (float, int)}
			},
			'layer': {'type': int},
			'layer_height': {'type': (float, int)},
			'file_progress': {
				'type': dict,
				'subfields': {
					'position': int,
					'total_size': int,
					'progress_pct': (float, int)
				}
			},
			'collection_time': {'type': (float, int)},
			'save_time': {'type': (float, int)},
			'hotend_temp': {'type': (float, int)},
			'bed_temp': {'type': (float, int)}
		}
		
		for field, validation in required_fields.items():
			if field not in state:
				return False, f"Missing required field: {field}"
				
			field_type = validation['type']
			if isinstance(field_type, tuple):
				if not isinstance(state[field], field_type):
					return False, f"Field {field} has wrong type"
			else:
				if not isinstance(state[field], validation['type']):
					return False, f"Field {field} has wrong type"
				
			if 'subfields' in validation:
				for subfield, subtype in validation['subfields'].items():
					if subfield not in state[field]:
						return False, f"Missing subfield {subfield} in {field}"
					if not isinstance(state[field][subfield], subtype):
						return False, f"Subfield {subfield} in {field} has wrong type"
		
		if state['file_progress']['total_size'] < 0:
			return False, "File size cannot be negative"
			
		if not (0 <= state['file_progress']['progress_pct'] <= 100):
			return False, "Progress percentage must be between 0 and 100"
			
		if state['layer'] < 0:
			return False, "Layer number cannot be negative"
				
		if not (-273.15 <= float(state['hotend_temp']) <= 500):
			return False, "Hotend temperature out of reasonable range"
			
		if not (-273.15 <= float(state['bed_temp']) <= 200):
			return False, "Bed temperature out of reasonable range"
			
		return True, None

	def _optimize_background_interval(self) -> float:
		try:
			interval = self.save_interval
			current_time = self.reactor.monotonic()
			time_since_extruder_change = current_time - self._last_extruder_change_time
			reduction_factor = 1.0
			
			if time_since_extruder_change < 20:
				extruder_factor = 0.3 + (time_since_extruder_change / 20.0) * 0.7
				reduction_factor = min(reduction_factor, extruder_factor)
			
			interval = interval * reduction_factor
			
			if len(self.state_history) >= 2:
				last_states = list(self.state_history)[-2:]
				pos_change = sum(abs(last_states[1]['position'][axis] - last_states[0]['position'][axis]) for axis in ['x', 'y', 'z'])
				if pos_change > 10:
					interval = interval * 0.75
				
				temp_change = abs(last_states[1]['hotend_temp'] - last_states[0]['hotend_temp'])
				if temp_change > 5:
					interval = interval * 0.75
			
			min_interval = 3.0 if time_since_extruder_change < 5 else 5.0
			return max(interval, min_interval)
			
		except Exception as e:
			return self.save_interval

	def _collect_current_state(self) -> Dict[str, Any]:
		  try:
			  eventtime = self.reactor.monotonic()
			  
			  try:
				  print_stats = self.printer.lookup_object('print_stats')
				  virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
				  print_stats_status = print_stats.get_status(eventtime)
				  sdcard_status = virtual_sdcard.get_status(eventtime)
				  extruder_status = self.extruder.get_status(eventtime)
				  toolhead_status = self.toolhead.get_status(eventtime)
				  heater_bed_status = self.heater_bed.get_status(eventtime) if self.heater_bed else {}
				  
				  fan_speeds = {}
				  for fan_name in self.part_cooling_fans:
					  try:
						  fan = self.printer.lookup_object(fan_name)
						  if fan:
							  fan_status = fan.get_status(eventtime)
							  fan_speeds[fan_name] = round(float(fan_status.get('speed', 0)), 3)
					  except Exception as e:
						  pass
				  
				  current_file = print_stats_status.get('filename', 'unknown')
				  file_position = sdcard_status.get('file_position', 0)
				  file_size = sdcard_status.get('file_size', 0)
				  
				  progress = (file_position / file_size * 100) if file_size > 0 else 0
				  
				  current_progress = {
					  'position': file_position,
					  'total_size': file_size,
					  'progress_pct': round(progress, 2)
				  }
				  
				  cur_pos = toolhead_status.get('position', [0., 0., 0., 0.])[:3]
				  hotend_temp = extruder_status.get('temperature', 0)
				  bed_temp = heater_bed_status.get('temperature', 0)
				  
				  state_info = {
					  'position': {
						  'x': round(float(cur_pos[0]), 3),
						  'y': round(float(cur_pos[1]), 3),
						  'z': round(float(cur_pos[2]), 3)
					  },
					  'fan_speeds': fan_speeds,
					  'layer': self.last_layer,
					  'layer_height': round(float(self.current_z_height), 3),
					  'file_progress': current_progress,
					  'active_extruder': extruder_status.get('active_extruder', 'extruder'),
					  'hotend_temp': round(float(hotend_temp), 1),
					  'bed_temp': round(float(bed_temp), 1),
					  'save_time': eventtime,
					  'current_file': current_file,
					  'collection_time': eventtime
				  }
				  return state_info
				  
			  except Exception as e:
				  return None
				  
		  except Exception as e:
			  return None
	
	def _get_move_buffer_status(self) -> dict:
		try:
			mcu = self.printer.lookup_object('mcu')
			status = mcu.get_status(self.reactor.monotonic())
			return {
				'moves_pending': status.get('moves_pending', 0),
				'min_move_time': status.get('min_move_time', 0),
				'max_move_time': status.get('max_move_time', 0)
			}
		except Exception as e:
			return {'moves_pending': 0, 'min_move_time': 0, 'max_move_time': 0}
	
	def _calculate_optimal_delay(self) -> int:
		try:
			toolhead = self.printer.lookup_object('toolhead')
			reactor = self.printer.get_reactor()
			mcu = self.printer.lookup_object('mcu')
			eventtime = reactor.monotonic()
			print_time = toolhead.get_last_move_time()
			est_print_time = mcu.estimated_print_time(eventtime)
			
			mcu_lag = print_time - est_print_time
			needed_intervals = max(1, int(mcu_lag / self.save_interval) + 1)
			optimal_delay = needed_intervals
				
			return min(optimal_delay, self.history_size - 1)
			
		except Exception as e:
			return self.save_delay

	def _save_current_state(self):
		if not self.is_active or self.resuming_print or not self.power_loss_recovery_enabled or self.save_variables is None:
			return
		
		try:
			optimal_delay = self._calculate_optimal_delay()
			
			if len(self.state_history) > optimal_delay:
				history_list = list(self.state_history)
				state_to_save = history_list[-(self.save_delay + 1)]
				
				buffer_status = self._get_move_buffer_status()
				state_to_save['mcu_status'] = buffer_status
				
				is_valid, error_msg = self._verify_state(state_to_save)
				if not is_valid:
					return
				
				state_json = json.dumps(state_to_save)
				escaped_json = state_json.replace('"', '\\"')
				self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=resume_meta_info VALUE="{escaped_json}"')
				
				self.last_save_time = self.reactor.monotonic()
				self._consecutive_failures = 0
				
		except Exception as e:
			pass
		  
	def _background_task(self, eventtime):
		try:
			last_save_attempt = getattr(self, '_last_save_attempt', 0)
			consecutive_failures = getattr(self, '_consecutive_failures', 0)
			
			print_stats = self.printer.lookup_object('print_stats')
			current_state = print_stats.get_status(eventtime)['state']
			printing = current_state == 'printing'
			
			if printing != self.is_active:
				self.is_active = printing
				if printing:
					self.state_history.clear()
					consecutive_failures = 0
			
			if self.is_active:
				if not self.power_loss_recovery_enabled:
					return eventtime + 1.0
					
				current_state = self._collect_current_state()
				if current_state:
					is_valid, error_msg = self._verify_state(current_state)
					if is_valid:
						self.state_history.append(current_state)
						consecutive_failures = 0
					else:
						consecutive_failures += 1
				
				should_save = False
				if self.time_based_enabled:
					time_since_last = eventtime - self.last_save_time
					interval = self._optimize_background_interval()
					should_save = time_since_last >= interval
				
				if consecutive_failures > 0:
					backoff = min(30, 2 ** consecutive_failures)
					if eventtime - last_save_attempt < backoff:
						should_save = False
				
				if should_save:
					self._last_save_attempt = eventtime
					self._save_current_state()
			
			self._consecutive_failures = consecutive_failures
			
			if not self.time_based_enabled or not printing:
				return eventtime + 1.0
			
			return eventtime + self._optimize_background_interval()
				
		except Exception as e:
			return eventtime + 1.0
			  
	def _handle_layer_change(self, gcmd):
		if not self.save_on_layer or not self.is_active:
			return
			
		try:
			self.last_layer = gcmd.get_int('LAYER', None)
			layer_height = gcmd.get_float('LAYER_HEIGHT', None)
			self._last_layer_change_time = self.reactor.monotonic()
			
			if layer_height is not None:
				self.current_z_height = layer_height
			
			self._save_current_state()
			
			if self.is_active and self.time_based_enabled:
				self.reactor.register_timer(self._background_task, self.reactor.monotonic())
				
		except Exception as e:
			pass
		
	def _handle_activate_extruder(self, eventtime):
		if not self.is_active:
			return
		try:
			self._last_extruder_change_time = self.reactor.monotonic()
			self._save_current_state()
			
			if self.is_active and self.time_based_enabled:
				self.reactor.register_timer(self._background_task, self.reactor.monotonic())
				
		except Exception as e:
			pass
				
	def _handle_connect(self):
		try:
			self.save_variables = self.printer.lookup_object('save_variables', None)
		except Exception as e:
			pass
	
	def _handle_ready(self):
		try:
			self.toolhead = self.printer.lookup_object('toolhead')
			self.extruder = self.printer.lookup_object('extruder')
			self.heater_bed = self.printer.lookup_object('heater_bed', None)
			self.reactor.register_timer(self._background_task, self.reactor.NOW)
		except Exception as e:
			pass
			
	def _reset_state(self):
		if self.save_variables is None:
			return
		try:
			empty_state = json.dumps({})
			self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=resume_meta_info VALUE="{empty_state}"')
			self.last_layer = 0
			self.last_save_time = 0
		except Exception as e:
			pass

	cmd_PLR_QUERY_SAVED_STATE_help = "Query the current status of the state saver"
	def cmd_PLR_QUERY_SAVED_STATE(self, gcmd):
		msg = ["PowerLossRecovery Status:"]
		msg.append(f"Active: {self.is_active}")
		msg.append(f"Power Loss Recovery: {'Enabled' if self.power_loss_recovery_enabled else 'Disabled'}")
		msg.append(f"Debug Mode: {'Enabled' if self.debug_mode else 'Disabled'}")
		msg.append(f"Time-based saving: {'Enabled (%ds interval)' % self.save_interval if self.time_based_enabled else 'Disabled'}")
		msg.append(f"Layer-based saving: {'Enabled (current layer: %d)' % self.last_layer if self.save_on_layer else 'Disabled'}")
		msg.append(f"History size: {self.history_size} (current: {len(self.state_history)})")
		msg.append(f"Save delay: {self.save_delay} states")
		
		try:
			val = self.save_variables.get_stored_variable('resume_meta_info')
			if val:
				saved_data = json.loads(val)
				progress_info = saved_data.get('file_progress', {})
				collection_time = saved_data.get('collection_time', 0)
				msg.extend([
					"",
					"Currently Saved State:",
					f"Collected at: {collection_time:.2f}",
					f"File: {saved_data.get('current_file', 'unknown')}",
					f"Layer: {saved_data.get('layer', 'unknown')}",
					f"Progress: {progress_info.get('progress_pct', 0):.2f}% " +
					f"(Position: {progress_info.get('position', 0)}/{progress_info.get('total_size', 0)} bytes)",
					"Position: X%.1f Y%.1f Z%.1f" % (
						saved_data.get('position', {}).get('x', 0),
						saved_data.get('position', {}).get('y', 0),
						saved_data.get('position', {}).get('z', 0)
					),
					f"Temperatures - Hotend: {saved_data.get('hotend_temp', 0):.1f}°C, Bed: {saved_data.get('bed_temp', 0):.1f}°C"
				])
		except Exception as e:
			pass
		gcmd.respond_info("\n".join(msg))
						
	cmd_PLR_SAVE_PRINT_STATE_help = "Manually save current printer state"
	def cmd_PLR_SAVE_PRINT_STATE(self, gcmd):
		self._save_current_state()
		gcmd.respond_info("Printer state saved")
		
	cmd_PLR_RESET_PRINT_DATA_help = "Clear all saved state data"
	def cmd_PLR_RESET_PRINT_DATA(self, gcmd):
		try:
			self._reset_state()
			gcmd.respond_info("PowerLossRecovery: All saved state data cleared")
		except Exception as e:
			gcmd.respond_info(f"Error clearing saved state: {str(e)}")	
			
	cmd_PLR_ENABLE_help = "Enable power loss recovery state saving"
	def cmd_PLR_ENABLE(self, gcmd):
		self.power_loss_recovery_enabled = True
		gcmd.respond_info("Power loss recovery enabled")
	
	cmd_PLR_DISABLE_help = "Disable power loss recovery state saving"
	def cmd_PLR_DISABLE(self, gcmd):
		self.power_loss_recovery_enabled = False
		gcmd.respond_info("Power loss recovery disabled")
	
	def _get_saved_state(self) -> Optional[Dict[str, Any]]:
		"""Retrieve the last saved state from the variables file."""
		try:
			if self.save_variables is None:
				return None
				
			eventtime = self.reactor.monotonic()
			variables = self.save_variables.get_status(eventtime)['variables']
			state_data = variables.get('resume_meta_info')
			
			if not state_data:
				return None
				
			is_valid, error_msg = self._verify_state(state_data)
			if not is_valid:
				return None
				
			return state_data
			
		except Exception as e:
			return None
			
	def _get_gcode_dir(self) -> str:
		try:
			virtual_sd = self.printer.lookup_object('virtual_sdcard')
			if virtual_sd:
				if hasattr(virtual_sd, 'sdcard_dirname'):
					return os.path.expanduser(virtual_sd.sdcard_dirname)
				if hasattr(virtual_sd, '_sdcard_dirname'):
					return os.path.expanduser(virtual_sd._sdcard_dirname)
		except Exception as e:
			pass
		return os.path.expanduser('~/gcode')
	
	cmd_PLR_RESUME_PRINT_help = "Create a modified gcode file for power loss recovery resume"
	def cmd_PLR_RESUME_PRINT(self, gcmd):
		try:
			self.resuming_print = True
			
			state_data = self._get_saved_state()
			if not state_data:
				gcmd.respond_info("No valid saved state found")
				return
				
			current_file = state_data.get('current_file')
			if not current_file:
				gcmd.respond_info("No filename found in saved state")
				return
				
			file_progress = state_data.get('file_progress', {})
			file_position = file_progress.get('position')
			if file_position is None:
				gcmd.respond_info("No file position found in saved state")
				return
				
			gcode_dir = self._get_gcode_dir()
			input_file = os.path.join(gcode_dir, current_file)
			
			if not os.path.exists(input_file):
				gcmd.respond_info(f"Original gcode file not found: {input_file}")
				return
				
			output_file = self._modify_gcode_file(input_file, file_position)
			if not output_file:
				gcmd.respond_info("Error creating modified gcode file")
				return
				
			try:
				virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
				if not virtual_sdcard:
					raise self.printer.command_error("virtual_sdcard not found")
				
				if hasattr(self, 'before_resume_gcode_lines') and self.before_resume_gcode_lines:
					for line in self.before_resume_gcode_lines:
						self.gcode.run_script_from_command(line)

				self._restore_fan_speeds(state_data)
				
				basename = os.path.basename(output_file)
				self.gcode.run_script_from_command(f'SDCARD_PRINT_FILE FILENAME="{basename}"')
				
				if hasattr(self, 'after_resume_gcode_lines') and self.after_resume_gcode_lines:
					for line in self.after_resume_gcode_lines:
						self.gcode.run_script_from_command(line)
				
				msg = [
					f"Created and started power loss recovery file: {basename}",
					f"Original file intact: {current_file}",
					f"Resume position: {file_position} ({file_progress.get('progress_pct', 0):.1f}%)"
				]
				gcmd.respond_info("\n".join(msg))
				
			except Exception as e:
				gcmd.respond_info(f"Error starting print: {str(e)}")
			
		except Exception as e:
			gcmd.respond_info(f"Error processing PLR resume: {str(e)}")
	
	def _modify_gcode_file(self, input_file: str, file_position: int) -> Optional[str]:
		try:
			saved_state = self._get_saved_state()
			if not saved_state:
				return None
				
			try:
				saved_z = saved_state['position']['z']
			except KeyError as e:
				return None
			
			base_name, ext = os.path.splitext(input_file)
			recovery_file = f"{base_name}_RECOVERY{ext}"
			
			if self.debug_mode:
				self._debug_log(f"Modifying {input_file} and saving as {recovery_file} to resume from pos {file_position}")
			
			SETUP_PLACEHOLDER = ";;;;; PLR_RESUME - INITIAL PRINTER SETUP STARTS ;;;;;"
			GCODE_PLACEHOLDER = ";;;;; PLR_RESUME - PRINT GCODE STARTS ;;;;;"
			
			found_setup = False
			found_gcode_start = False
			current_position = 0
			last_layer_z = None
			
			layer_block_lines = []
			in_layer_block = False
			
			with open(input_file, 'r') as infile, open(recovery_file, 'w') as outfile:
				in_setup_section = False
				in_comment_block = False
				in_executable_block = False
				comment_buffer = []
				
				for line in infile:
					current_position += len(line)
					stripped_line = line.strip()
					
					if "; EXECUTABLE_BLOCK_START" in line:
						in_executable_block = True
						outfile.write(line)
						continue
					elif "; EXECUTABLE_BLOCK_END" in line:
						in_executable_block = False
						outfile.write(line)
						continue
						
					if current_position < file_position:
						if stripped_line == ";LAYER_CHANGE":
							in_layer_block = True
							layer_block_lines = [line]
						elif in_layer_block:
							layer_block_lines.append(line)
							if stripped_line.startswith(";Z:"):
								try:
									last_layer_z = float(stripped_line[3:])
								except ValueError:
									pass
							elif not stripped_line.startswith(";"):
								in_layer_block = False
								layer_block_lines = []
					
					if not in_executable_block:
						if not in_comment_block and stripped_line.startswith(';') and not stripped_line.startswith(';;'):
							in_comment_block = True
							comment_buffer = [line]
							continue
						elif in_comment_block:
							if stripped_line.startswith(';'):
								comment_buffer.append(line)
								continue
							else:
								in_comment_block = False
								for comment_line in comment_buffer:
									outfile.write(comment_line)
								comment_buffer = []
					
					if not in_comment_block or in_executable_block:
						if SETUP_PLACEHOLDER in line:
							found_setup = True
							in_setup_section = True
							outfile.write(line)
							
							if hasattr(self, 'restart_gcode_lines') and self.restart_gcode_lines:
								for gcode_line in self.restart_gcode_lines:
									outfile.write(f"{gcode_line}\n")
								
								z_height = last_layer_z if last_layer_z is not None else saved_z
								z_restore_gcode = f"G1 Z{z_height:.3f} F3000 ; Restore Z height from last layer"
								outfile.write(f"{z_restore_gcode}\n")
							
							outfile.write(f"{GCODE_PLACEHOLDER}\n")
							found_gcode_start = True
							in_setup_section = False
							continue
						elif GCODE_PLACEHOLDER in line:
							continue
					
					if not found_setup:
						outfile.write(line)
					elif found_gcode_start:
						if current_position >= file_position:
							outfile.write(line)
					elif in_executable_block:
						outfile.write(line)
					elif in_comment_block and not in_setup_section:
						outfile.write(line)
					
				if comment_buffer:
					for line in comment_buffer:
						outfile.write(line)
			
			if not (found_setup and found_gcode_start):
				if self.debug_mode:
					self._debug_log("Required placeholders not found in gcode file")
				if os.path.exists(recovery_file):
					os.remove(recovery_file)
				return None
				
			return recovery_file
			
		except Exception as e:
			if self.debug_mode:
				self._debug_log(f"Error modifying gcode file: {str(e)}")
			try:
				if os.path.exists(recovery_file):
					os.remove(recovery_file)
			except:
				pass
			return None