# Copyright (c) 2017 Ansible Tower by Red Hat
# All Rights Reserved.

# Python
import logging
import json
import requests
from copy import copy

# loggly
import traceback

from requests_futures.sessions import FuturesSession

# custom
from django.conf import settings as django_settings
from django.utils.log import NullHandler

# AWX external logging handler, generally designed to be used
# with the accompanying LogstashHandler, derives from python-logstash library
# Non-blocking request accomplished by FuturesSession, similar
# to the loggly-python-handler library (not used)

# Translation of parameter names to names in Django settings
PARAM_NAMES = {
    'host': 'LOG_AGGREGATOR_HOST',
    'port': 'LOG_AGGREGATOR_PORT',
    'message_type': 'LOG_AGGREGATOR_TYPE',
    'username': 'LOG_AGGREGATOR_USERNAME',
    'password': 'LOG_AGGREGATOR_PASSWORD',
    'enabled_loggers': 'LOG_AGGREGATOR_LOGGERS',
    'indv_facts': 'LOG_AGGREGATOR_INDIVIDUAL_FACTS',
    'enabled_flag': 'LOG_AGGREGATOR_ENABLED',
}


def unused_callback(sess, resp):
    pass


class HTTPSNullHandler(NullHandler):
    "Placeholder null handler to allow loading without database access"

    def __init__(self, host, **kwargs):
        return super(HTTPSNullHandler, self).__init__()


class BaseHTTPSHandler(logging.Handler):
    def __init__(self, fqdn=False, **kwargs):
        super(BaseHTTPSHandler, self).__init__()
        self.fqdn = fqdn
        self.async = kwargs.get('async', True)
        for fd in PARAM_NAMES:
            setattr(self, fd, kwargs.get(fd, None))
        if self.async:
            self.session = FuturesSession()
        else:
            self.session = requests.Session()
        self.add_auth_information()

    @classmethod
    def from_django_settings(cls, settings, *args, **kwargs):
        for param, django_setting_name in PARAM_NAMES.items():
            kwargs[param] = getattr(settings, django_setting_name, None)
        return cls(*args, **kwargs)

    def get_full_message(self, record):
        if record.exc_info:
            return '\n'.join(traceback.format_exception(*record.exc_info))
        else:
            return record.getMessage()

    def add_auth_information(self):
        if self.message_type == 'logstash':
            if not self.username:
                # Logstash authentication not enabled
                return
            logstash_auth = requests.auth.HTTPBasicAuth(self.username, self.password)
            self.session.auth = logstash_auth
        elif self.message_type == 'splunk':
            auth_header = "Splunk %s" % self.password
            headers = {
                "Authorization": auth_header,
                "Content-Type": "application/json"
            }
            self.session.headers.update(headers)

    def get_http_host(self):
        host = self.host
        if not host.startswith('http'):
            host = 'http://%s' % self.host
        if self.port != 80 and self.port is not None:
            host = '%s:%s' % (host, str(self.port))
        return host

    def get_post_kwargs(self, payload_input):
        if self.message_type == 'splunk':
            # Splunk needs data nested under key "event"
            if not isinstance(payload_input, dict):
                payload_input = json.loads(payload_input)
            payload_input = {'event': payload_input}
        if isinstance(payload_input, dict):
            payload_str = json.dumps(payload_input)
        else:
            payload_str = payload_input
        if self.async:
            return dict(data=payload_str, background_callback=unused_callback)
        else:
            return dict(data=payload_str)

    def skip_log(self, logger_name):
        if self.host == '' or (not self.enabled_flag):
            return True
        if not logger_name.startswith('awx.analytics'):
            # Tower log emission is only turned off by enablement setting
            return False
        return self.enabled_loggers is None or logger_name.split('.')[-1] not in self.enabled_loggers

    def emit(self, record):
        """
            Emit a log record.  Returns a list of zero or more
            ``concurrent.futures.Future`` objects.

            When ``self.async`` is True, the list will contain one
            Future object for each HTTP request made.  When ``self.async`` is
            False, the list will be empty.

            See:
            https://docs.python.org/3/library/concurrent.futures.html#future-objects
            http://pythonhosted.org/futures/
        """
        if self.skip_log(record.name):
            return []
        try:
            payload = self.format(record)

            # Special action for System Tracking, queue up multiple log messages
            if self.indv_facts:
                payload_data = json.loads(payload)
                if record.name.startswith('awx.analytics.system_tracking'):
                    module_name = payload_data['module_name']
                    if module_name in ['services', 'packages', 'files']:
                        facts_dict = payload_data.pop(module_name)
                        async_futures = []
                        for key in facts_dict:
                            fact_payload = copy(payload_data)
                            fact_payload.update(facts_dict[key])
                            if self.async:
                                async_futures.append(self._send(fact_payload))
                            else:
                                self._send(fact_payload)
                        return async_futures

            if self.async:
                return [self._send(payload)]

            self._send(payload)
            return []
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)

    def _send(self, payload):
        return self.session.post(self.get_http_host(),
                                 **self.get_post_kwargs(payload))


class HTTPSHandler(object):

    def __new__(cls, *args, **kwargs):
        return BaseHTTPSHandler.from_django_settings(django_settings, *args, **kwargs)
