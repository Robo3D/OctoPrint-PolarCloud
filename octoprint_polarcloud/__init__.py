# coding=utf-8

from __future__ import absolute_import

__author__ = "Mark Walker (markwal@hotmail.com)"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2017 Mark Walker"

"""
    This file is part of OctoPrint-PolarCloud.

    OctoPrint-PolarCloud is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    OctoPrint-PolarCloud is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with OctoPrint-PolarCloud.  If not, see <http://www.gnu.org/licenses/>.
"""

import os
import threading
import logging
import uuid
import Queue
import base64
import datetime

from OpenSSL import crypto
from socketIO_client import SocketIO, LoggingNamespace, TimeoutError, ConnectionError
import sarge
import flask
import requests

import octoprint.plugin
import octoprint.util
from octoprint.util import get_exception_string
from octoprint.events import Events

logging.getLogger('socketIO-client').setLevel(logging.DEBUG)
logging.basicConfig()

# what's a mac address we can use as an identifier?
def get_mac():
	return ':'.join(('%012X' % uuid.getnode())[i:i+2] for i in range(0, 12, 2))

# what's the likely ip address for the local UI?
def get_ip():
	return octoprint.util.address_for_client("google.com", 80)

# do a dictionary lookup and return an empty string for any missing key
# rather than throw MissingKey
def str_safe_get(dictionary, *keys):
	return reduce(lambda d, k: d.get(k) if isinstance(d, dict) else "", keys, dictionary)

# return true if each of the list of keys are in the dictionary, otherwise false
def has_all(dictionary, *keys):
	for key in keys:
		if not key in dictionary:
			return False
	return True

class PolarcloudPlugin(octoprint.plugin.SettingsPlugin,
                       octoprint.plugin.AssetPlugin,
                       octoprint.plugin.TemplatePlugin,
                       octoprint.plugin.StartupPlugin,
                       octoprint.plugin.SimpleApiPlugin):
	PSTATE_IDLE = "0"
	PSTATE_SERIAL = "1"         # Printing a local print over serial
	PSTATE_PREPARING = "2"      # Preparing a cloud print (slicing)
	PSTATE_PRINTING = "3"       # Printing a cloud print
	PSTATE_PAUSED = "4"
	PSTATE_POSTPROCESSING = "5" # Performing post-print operations
	PSTATE_CANCELING = "6"      # Canceling a print originated from the cloud
	PSTATE_COMPLETE = "7"       # Completed a print originated from the cloud
	PSTATE_UPDATING = "8"       # Busy updating OctoPrint and/or plugins
	PSTATE_COLDPAUSED = "9"
	PSTATE_CHANGINGFILAMENT = "10"
	PSTATE_TCPIP = "11"         # Printing a local print over TCP/IP
	PSTATE_ERROR = "12"

	def __init__(self):
		self._serial = None
		self._socket = None
		self._challenge = None
		self._task_queue = Queue.Queue()
		self._polar_status_worker = None
		self._upload_location = {}
		self._update_interval = 60
		self._cloud_print = False
		self._job_id = "123"
		self._pstate = self.PSTATE_IDLE # only applies if _cloud_print
		self._pstate_counter = 0

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self, *args, **kwargs):
		self._logger.info("get_settings_defaults")
		return dict(
			service="localhost",
			serial=None,
			printer_type=None,
			email=""
		)

	##~~ AssetPlugin mixin

	def get_assets(self, *args, **kwargs):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/polarcloud.js"],
			css=["css/polarcloud.css"],
			less=["less/polarcloud.less"]
		)

	##~~ Softwareupdate hook

	def get_update_information(self, *args, **kwargs):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		return dict(
			polarcloud=dict(
				displayName="Polarcloud Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="markwal",
				repo="OctoPrint-PolarCloud",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/markwal/OctoPrint-PolarCloud/archive/{target_version}.zip"
			)
		)

	##~~ StartupPlugin mixin

	def on_after_startup(self, *args, **kwargs):
		self._logger.setLevel(logging.DEBUG)
		self._logger.debug("on_after_startup")
		self._get_keys()
		self._snapshot_url = self._settings.global_get(["webcam", "snapshot"])
		self._serial = self._settings.get(['serial'])
		if self._serial:
			self._create_socket()

	##~~ utility functions

	def _get_job_id(self):
		if self._printer.is_printing():
			return self._job_id
		else:
			return '0'

	def _valid_packet(self, data):
		if not self._serial or self._serial != data.get("serialNumber", ""):
			self._logger.debug("Serial number is '{}'".format(repr(self._serial)))
			self._logger.debug("Ignoring message to '{}'".format(data.get("serialNumber", "")))
			return False
		return True

	##~~ polar communication

	def _create_socket(self):
		self._logger.debug("_create_socket")

		# Create socket and set up event handlers
		try:
			self._socket = SocketIO(self._settings.get(['service']), 8080, Namespace=LoggingNamespace, verify=True, wait_for_connection=False)
		except (TimeoutError, ConnectionError, StopIteration):
			self._socket = None
			self._logger.exception('Unable to open socket {}'.format(get_exception_string()))
			return

		# Register all the socket messages
		self._socket.on('registerResponse', self._on_register_response)
		self._socket.on('welcome', self._on_welcome)
		self._socket.on('getUrlResponse', self._on_get_url_response)
		self._socket.on('cancel', self._on_cancel)
		self._socket.on('command', self._on_command)
		self._socket.on('pause', self._on_pause)
		self._socket.on('print', self._on_print)
		self._socket.on('resume', self._on_resume)
		self._socket.on('temperature', self._on_temperature)
		self._socket.on('update', self._on_update)

		# spin up the status thread
		self._start_polar_status()

	def _start_polar_status(self):
		if not self._polar_status_worker:
			self._polar_status_worker = threading.Thread(target=self._polar_status_heartbeat)
			self._polar_status_worker.daemon = True
			self._polar_status_worker.start()

	def _get_keys(self):
		data_folder = self.get_plugin_data_folder()
		key_filename = os.path.join(data_folder, 'p3d_key')
		self._logger.debug('key_filename: {}'.format(key_filename))
		if not os.path.isfile(key_filename):
			self._logger.debug('Generating key pair')
			key = crypto.PKey()
			key.generate_key(crypto.TYPE_RSA, 2048)
			with open(key_filename, 'w') as f:
				f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))
		try:
			with open(key_filename) as f:
				key = f.read()
			self.key = crypto.load_privatekey(crypto.FILETYPE_PEM, key)
		except:
			self.key = None
			self._logger.error("Unable to generate or access key.")

	# map from OctoPrint's notion of state to Polar's notion
	def _polar_status_from_state(self):
		state_mapping = {
			"OPEN_SERIAL": self.PSTATE_ERROR,
			"DETECT_SERIAL": self.PSTATE_ERROR,
			"DETECT_BAUDRATE": self.PSTATE_ERROR,
			"CONNECTING": self.PSTATE_ERROR,
			"OPERATIONAL": self.PSTATE_IDLE,
			"PRINTING": self.PSTATE_SERIAL,
			"PAUSED": self.PSTATE_PAUSED,
			"CLOSED": self.PSTATE_ERROR,
			"ERROR": self.PSTATE_ERROR,
			"CLOSED_WITH_ERROR": self.PSTATE_ERROR,
			"TRANSFERING_FILE": self.PSTATE_SERIAL,
			"OFFLINE": self.PSTATE_ERROR,
			"UNKNOWN": self.PSTATE_ERROR,
			"NONE": self.PSTATE_ERROR
		}
		return state_mapping[self._printer.get_state_id()]

	def _current_status(self):
		temps = self._printer.get_current_temperatures()
		self._logger.debug("{}".format(temps))
		status = {
			"serialNumber": self._serial,
			"status": self._polar_status_from_state(),
			"tool0": temps['tool0']['actual'] if 'tool0' in temps else 0.0,
			"tool1": temps['tool1']['actual'] if 'tool1' in temps else 0.0,
			"bed": temps['bed']['actual'] if 'bed' in temps else 0.0,
			"targetTool0": temps['tool0']['target'] if 'tool0' in temps else 0.0,
			"targetTool1": temps['tool1']['target'] if 'tool1' in temps else 0.0,
			"targetBed": temps['bed']['target'] if 'bed' in temps else 0.0,
			"jobId": self._get_job_id(),
			"protocol": "2",
			"progress": "",
			"progressDetail": "",
			"estimatedTime": "0",
			"filamentUsed": "0",
			"startTime": "0",
			"printSeconds": "0",
			"bytesRead": "0",
			"fileSize": "0",
			"file": "",          # url for cloud stl
			"config": "",        # url for cloud config.ini
			"sliceDetails": "",  # Cura_SteamEngine output
			"securityCode": ""   # three colors
		}
		if self._printer.is_printing():
			data = self._printer.get_current_data()
			status["progress"] = str_safe_get(data, 'state', 'text')
			status["progressDetail"] = "Printing Job: {} Percent Complete: {:0.1f}%".format(
				str_safe_get(data, 'file', 'name'), str_safe_get(data, 'progress', 'completion'))
			status["estimatedTime"] = str_safe_get(data, "job", "estimatedPrintTime")
			status["filamentUsed"] = str_safe_get(data, "job", "filament", "length")
			status["printSeconds"] = str_safe_get(data, "progress", "printTime")
			status["startTime"] = (datetime.datetime.now() - datetime.timedelta(seconds=int(status["printSeconds"]))).isoformat()
			status["bytesRead"] = str_safe_get(data, "progress", "filepos")
			status["fileSize"] = str_safe_get(data, "job", "file", "size")
		return status

	# thread to update the polar cloud with current status periodically
	def _polar_status_heartbeat(self):
		try:
			task = self._task_queue.get_nowait()
			task()
		except Queue.Empty:
			pass
		self._socket.wait(seconds=5)
		while True:
			try:
				if self._serial:
					status = self._current_status()
					self._socket.emit("status", status)

					# reset update interval to slow if we're not printing anymore
					# we do it here so we get one quick update when it changes
					if not self._cloud_print and not self._printer.is_printing():
						self._update_interval = 60

				# wait for _update_interval seconds in 1 second chunks so that
				# _update_interval can more quickly change when we start
				# printing and so we get around to queued tasks
				for i in range(self._update_interval):
					try:
						task = self._task_queue.get_nowait()
						task()
					except Queue.Empty:
						pass
					self._socket.wait(seconds=1)

				if self._serial:
					self._upload_snapshot()

			except Exception as e:
				import traceback
				self._logger.warn("polar_status exception: {}".format(traceback.format_exc()))

	#~~ time-lapse and snapshots to cloud

	def _create_timelapse(self):
		# TODO figure out how to timebox/sizebox the timelapse
		'gst-launch-1.0 qtmux name=mux ! filesink location="$ARG2"  multifilesrc location="$ARG1" index=1 caps="image/jpeg,framerate=\(fraction\)12/1" ! jpegdec ! videoconvert ! videorate ! x264enc ! mux .'

	def _ensure_upload_url(self, upload_type):
		if not self._snapshot_url:
			return False
		if not upload_type in self._upload_location or datetime.datetime.now() > self._upload_location[upload_type]['expires']:
			self._get_url(upload_type, self._get_job_id())
			return False
		return True

	def _ensure_idle_upload_url(self):
		self._ensure_upload_url('idle')

	def _upload_snapshot(self):
		self._logger.debug("_upload_snapshot")
		upload_type = 'idle' # TODO cloud print
		if not self._ensure_upload_url(upload_type):
			return
		try:
			loc = self._upload_location[upload_type]
			r = requests.get(self._snapshot_url, timeout=5)
			r.raise_for_status()
		except Exception as e:
			self._logger.exception("Could not capture image from {}".format(self._snapshot_url))

		try:
			p = requests.post(loc['url'], data=loc['fields'], files={'file': ('image.jpg', r.content)})
			self._logger.debug("{}".format(p.text))
			p.raise_for_status()
			t = p.status_code
			self._logger.debug("{}: {}".format(t, p.content))

			self._logger.debug("Image captured from {}".format(self._snapshot_url))
		except Exception as e:
			self._logger.exception("Could not post snapshot to PolarCloud")

	#~~ getUrl -> polar: getUrlResponse

	def _on_get_url_response(self, response, *args, **kwargs):
		if not self._valid_packet(response):
			return
		self._logger.debug('getUrlResponse {}'.format(repr(response)))
		if not has_all(response, 'status'):
			self._logger.warn('getUrlResponse lacks status property')
			return
		if not response['status'] == 'SUCCESS':
			self._logger.warn('Failed to get upload url: {} {}'
				.format(response['status'], response.get('message', '')))
			return
		if not has_all(response, 'type', 'expires', 'url', 'maxSize', 'fields'):
			self._logger.warn('getUrlResponse lacks a required property')
		response["expires"] = (datetime.datetime.now() + datetime.timedelta(seconds=int(response.get("expires", 0))))
		self._upload_location[response.get('type', 'idle')] = response
		self._logger.debug('response["type"] = {}', response.get('type', ''))
		if response.get('type', '') == 'idle':
			self._task_queue.put(self._upload_snapshot)

	# get upload url from the cloud
	# url_type - 'idle' | 'printing' | 'timelapse'
	#	'printing'/'timelapse' for cloud initiated print only
	# job_id - cloud assigned print job id ('123' for local print)
	def _get_url(self, url_type, job_id):
		self._logger.debug('getUrl')
		self._socket.emit('getUrl', {
			'serialNumber': self._serial,
			'method': 'post',
			'type': url_type,
			'jobId': job_id
		})

	#~~ polar: welcome -> hello

	def _on_welcome(self, welcome, *args, **kwargs):
		self._logger.debug('_on_welcome: {}'.format(repr(welcome)))
		if 'challenge' in welcome:
			self._challenge = welcome['challenge']
			self._task_queue.put(self._hello)
			self._start_polar_status()

	def _hello(self):
		self._logger.debug('hello')
		if self._serial and self._challenge:
			self._logger.debug('emit hello')
			self._socket.emit('hello', {
				'serialNumber': self._serial,
				'signature': base64.b64encode(crypto.sign(self.key, self._challenge, b'sha256')),
				'MAC': get_mac(),
				'localIp': get_ip(),
				'protocol': '2'
			})
			self._task_queue.put(self._ensure_idle_upload_url)
		else:
			self._logger.debug('skip emit hello, serial: {}'.format(self._serial))

	#~~ register -> polar: registerReponse

	def _on_register_response(self, response, *args, **kwargs):
		self._logger.debug('on_register_response: {}'.format(repr(response)))
		if 'serialNumber' in response:
			self._serial = response['serialNumber']
			self._settings.set(['serial'], self._serial)
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'serial',
				'serial': response['serialNumber']
			})
			if self._challenge:
				self._task_queue.put(self._hello)
		else:
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'registration_failed'
			})

	def _register(self, email, pin):
		self._get_keys()
		if not self.key:
			self._logger.info("Can't register because unable to generate signing key")
			return False

		if not self._socket:
			self._create_socket()
		if not self._socket:
			self._logger.info("Can't register because unable to communicate with Polar Cloud")
			return False

		self._logger.info("emit register")
		self._socket.emit("register", {
			"mfg": "op",
			"email": email,
			"pin": pin,
			"publicKey": crypto.dump_publickey(crypto.FILETYPE_PEM, self.key),
			"myInfo": {
				"MAC": get_mac(),
				"protocolVersion": "2"
				# "rotateImg": 1,
				# "camOff": 1,
				# "printerType": "MakerBot Replicator 1 Dual",
				# "serialNumber": "pb000103",
				# "timestamp": ""
			}
		})
		return True

	#~~ cancel

	def _on_cancel(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._printer.cancel_print()

	#~~ command

	def _on_command(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._printer.commands(data.get("command", ""))

	#~~ pause

	def _on_pause(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		# TODO data['type'] = filament, cold, pause
		self._printer.pause_print()

	#~~ print

	def _on_print(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		# TODO cloud print

	#~~ resume

	def _on_resume(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._printer.resume_print()

	#~~ temperature

	def _on_temperature(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		for key in data:
			if re.match("(?bed)|(?tool[0-9]+)", key):
				self._logger.debug("set_temperature {} to {}", key, data['key'])
				self._printer.set_temperature(key, data['key'])

	#~~ update

	def _on_update(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		# TODO software update

	#~~ job
	def _job(self, job_id, state):
		self._logger.debug('job')
		if self._serial:
			self._socket.emit('job', {
				'serialNumber': self._serial,
				'jobId': job_id,
				'state': state
			})
		pass

	#~~ setVersion
	def _set_version(self):
		self._logger.debug('setVersion')
		if self._serial:
			from octoprint._version import get_versions
			octoprint_version = get_versions()["version"]
			self._socket.emit('setVersion', {
				'serialNumber': self._serial,
				'runningVersion': octoprint_version,
				'latestVersion': octoprint_version # TODO interrogate the softwareupdate plugin
			})
		pass

	#~~ EventHandlerPlugin mixin

	def on_event(self, event, payload):
		if event == Events.PRINT_CANCELLED:
			if self._cloud_print:
				self._pstate = self.PSTATE_CANCELLING
				self._pstate_counter = 3
		if event == Events.PRINT_STARTED or event == Events.PRINT_RESUMED:
			self._update_interval = 10

	#~~ SimpleApiPlugin mixin

	def get_api_commands(self, *args, **kwargs):
		return dict(
			register=[]
		)

	def is_api_adminonly(self, *args, **kwargs):
		return True

	def on_api_command(self, command, data):
		self._logger.info('on_api_command {}'.format(repr(data)))
		status='FAIL'
		message=''
		if command == 'register' and 'email' in data and 'pin' in data:
			if self._register(data['email'], data['pin']):
				status = 'WAIT'
				message = "Waiting for response from Polar Cloud"
			else:
				message = "Unable to communicate with Polar Cloud"
		else:
			message = "Unable to understand command"
		return flask.jsonify({'status': status, 'message': message})

# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "PolarCloud"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PolarcloudPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

