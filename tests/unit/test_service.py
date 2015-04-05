# Copyright (c) 2014 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import mock
import os
import six
import tempfile
import testtools
import time

from concurrent.futures import Future
from hashlib import md5
from mock import Mock, PropertyMock
from six.moves.queue import Queue, Empty as QueueEmptyError
from six import BytesIO
from time import sleep

import swiftclient
import swiftclient.utils as utils
from swiftclient.client import Connection, ClientException
from swiftclient.service import (
    SwiftService, SwiftError, SwiftUploadObject
)


clean_os_environ = {}
environ_prefixes = ('ST_', 'OS_')
for key in os.environ:
    if any(key.startswith(m) for m in environ_prefixes):
        clean_os_environ[key] = ''


if six.PY2:
    import __builtin__ as builtins
else:
    import builtins


class TestSwiftPostObject(testtools.TestCase):

    def setUp(self):
        super(TestSwiftPostObject, self).setUp()
        self.spo = swiftclient.service.SwiftPostObject

    def test_create(self):
        spo = self.spo('obj_name')

        self.assertEqual(spo.object_name, 'obj_name')
        self.assertEqual(spo.options, None)

    def test_create_with_invalid_name(self):
        # empty strings are not allowed as names
        self.assertRaises(SwiftError, self.spo, '')

        # names cannot be anything but strings
        self.assertRaises(SwiftError, self.spo, 1)


class TestSwiftReader(testtools.TestCase):

    def setUp(self):
        super(TestSwiftReader, self).setUp()
        self.sr = swiftclient.service._SwiftReader
        self.md5_type = type(md5())

    def test_create(self):
        sr = self.sr('path', 'body', {})

        self.assertEqual(sr._path, 'path')
        self.assertEqual(sr._body, 'body')
        self.assertEqual(sr._content_length, None)
        self.assertEqual(sr._expected_etag, None)

        self.assertNotEqual(sr._actual_md5, None)
        self.assertTrue(isinstance(sr._actual_md5, self.md5_type))

    def test_create_with_large_object_headers(self):
        # md5 should not be initialized if large object headers are present
        sr = self.sr('path', 'body', {'x-object-manifest': 'test'})
        self.assertEqual(sr._path, 'path')
        self.assertEqual(sr._body, 'body')
        self.assertEqual(sr._content_length, None)
        self.assertEqual(sr._expected_etag, None)
        self.assertEqual(sr._actual_md5, None)

        sr = self.sr('path', 'body', {'x-static-large-object': 'test'})
        self.assertEqual(sr._path, 'path')
        self.assertEqual(sr._body, 'body')
        self.assertEqual(sr._content_length, None)
        self.assertEqual(sr._expected_etag, None)
        self.assertEqual(sr._actual_md5, None)

    def test_create_with_content_length(self):
        sr = self.sr('path', 'body', {'content-length': 5})

        self.assertEqual(sr._path, 'path')
        self.assertEqual(sr._body, 'body')
        self.assertEqual(sr._content_length, 5)
        self.assertEqual(sr._expected_etag, None)

        self.assertNotEqual(sr._actual_md5, None)
        self.assertTrue(isinstance(sr._actual_md5, self.md5_type))

        # Check Contentlength raises error if it isnt an integer
        self.assertRaises(SwiftError, self.sr, 'path', 'body',
                          {'content-length': 'notanint'})

    def test_iterator_usage(self):
        def _consume(sr):
            for _ in sr:
                pass

        sr = self.sr('path', BytesIO(b'body'), {})
        _consume(sr)

        # Check error is raised if expected etag doesnt match calculated md5.
        # md5 for a SwiftReader that has done nothing is
        # d41d8cd98f00b204e9800998ecf8427e  i.e md5 of nothing
        sr = self.sr('path', BytesIO(b'body'), {'etag': 'doesntmatch'})
        self.assertRaises(SwiftError, _consume, sr)

        sr = self.sr('path', BytesIO(b'body'),
                     {'etag': '841a2d689ad86bd1611447453c22c6fc'})
        _consume(sr)

        # Check error is raised if SwiftReader doesnt read the same length
        # as the content length it is created with
        sr = self.sr('path', BytesIO(b'body'), {'content-length': 5})
        self.assertRaises(SwiftError, _consume, sr)

        sr = self.sr('path', BytesIO(b'body'), {'content-length': 4})
        _consume(sr)

        # Check that the iterator generates expected length and etag values
        sr = self.sr('path', ['abc'.encode()] * 3, {})
        _consume(sr)
        self.assertEqual(sr._actual_read, 9)
        self.assertEqual(sr._actual_md5.hexdigest(),
                         '97ac82a5b825239e782d0339e2d7b910')


class _TestServiceBase(testtools.TestCase):
    def _assertDictEqual(self, a, b, m=None):
        # assertDictEqual is not available in py2.6 so use a shallow check
        # instead
        if hasattr(self, 'assertDictEqual'):
            self.assertDictEqual(a, b, m)
        else:
            self.assertTrue(isinstance(a, dict))
            self.assertTrue(isinstance(b, dict))
            self.assertEqual(len(a), len(b), m)
            for k, v in a.items():
                self.assertTrue(k in b, m)
                self.assertEqual(b[k], v, m)

    def _get_mock_connection(self, attempts=2):
        m = Mock(spec=Connection)
        type(m).attempts = PropertyMock(return_value=attempts)
        type(m).auth_end_time = PropertyMock(return_value=4)
        return m

    def _get_queue(self, q):
        # Instead of blocking pull items straight from the queue.
        # expects at least one item otherwise the test will fail.
        try:
            return q.get_nowait()
        except QueueEmptyError:
            self.fail('Expected item in queue but found none')

    def _get_expected(self, update=None):
        expected = self.expected.copy()
        if update:
            expected.update(update)

        return expected


class TestServiceDelete(_TestServiceBase):
    def setUp(self):
        super(TestServiceDelete, self).setUp()
        self.opts = {'leave_segments': False, 'yes_all': False}
        self.exc = Exception('test_exc')
        # Base response to be copied and updated to matched the expected
        # response for each test
        self.expected = {
            'action': None,   # Should be string in the form delete_XX
            'container': 'test_c',
            'object': 'test_o',
            'attempts': 2,
            'response_dict': {},
            'success': None   # Should be a bool
        }

    def test_delete_segment(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        expected_r = self._get_expected({
            'action': 'delete_segment',
            'object': 'test_s',
            'success': True,
        })

        r = SwiftService._delete_segment(mock_conn, 'test_c', 'test_s', mock_q)

        mock_conn.delete_object.assert_called_once_with(
            'test_c', 'test_s', response_dict={}
        )
        self._assertDictEqual(expected_r, r)
        self._assertDictEqual(expected_r, self._get_queue(mock_q))

    def test_delete_segment_exception(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        mock_conn.delete_object = Mock(side_effect=self.exc)
        expected_r = self._get_expected({
            'action': 'delete_segment',
            'object': 'test_s',
            'success': False,
            'error': self.exc,
            'traceback': mock.ANY,
            'error_timestamp': mock.ANY
        })

        before = time.time()
        r = SwiftService._delete_segment(mock_conn, 'test_c', 'test_s', mock_q)
        after = time.time()

        mock_conn.delete_object.assert_called_once_with(
            'test_c', 'test_s', response_dict={}
        )
        self._assertDictEqual(expected_r, r)
        self._assertDictEqual(expected_r, self._get_queue(mock_q))
        self.assertGreaterEqual(r['error_timestamp'], before)
        self.assertLessEqual(r['error_timestamp'], after)
        self.assertTrue('Traceback' in r['traceback'])

    def test_delete_object(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        mock_conn.head_object = Mock(return_value={})
        expected_r = self._get_expected({
            'action': 'delete_object',
            'success': True
        })

        s = SwiftService()
        r = s._delete_object(mock_conn, 'test_c', 'test_o', self.opts, mock_q)

        mock_conn.head_object.assert_called_once_with('test_c', 'test_o')
        mock_conn.delete_object.assert_called_once_with(
            'test_c', 'test_o', query_string=None, response_dict={}
        )
        self._assertDictEqual(expected_r, r)

    def test_delete_object_exception(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        mock_conn.delete_object = Mock(side_effect=self.exc)
        expected_r = self._get_expected({
            'action': 'delete_object',
            'success': False,
            'error': self.exc,
            'traceback': mock.ANY,
            'error_timestamp': mock.ANY
        })
        # _delete_object doesnt populate attempts or response dict if it hits
        # an error. This may not be the correct behaviour.
        del expected_r['response_dict'], expected_r['attempts']

        before = time.time()
        s = SwiftService()
        r = s._delete_object(mock_conn, 'test_c', 'test_o', self.opts, mock_q)
        after = time.time()

        mock_conn.head_object.assert_called_once_with('test_c', 'test_o')
        mock_conn.delete_object.assert_called_once_with(
            'test_c', 'test_o', query_string=None, response_dict={}
        )
        self._assertDictEqual(expected_r, r)
        self.assertGreaterEqual(r['error_timestamp'], before)
        self.assertLessEqual(r['error_timestamp'], after)
        self.assertTrue('Traceback' in r['traceback'])

    def test_delete_object_slo_support(self):
        # If SLO headers are present the delete call should include an
        # additional query string to cause the right delete server side
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        mock_conn.head_object = Mock(
            return_value={'x-static-large-object': True}
        )
        expected_r = self._get_expected({
            'action': 'delete_object',
            'success': True
        })

        s = SwiftService()
        r = s._delete_object(mock_conn, 'test_c', 'test_o', self.opts, mock_q)

        mock_conn.head_object.assert_called_once_with('test_c', 'test_o')
        mock_conn.delete_object.assert_called_once_with(
            'test_c', 'test_o',
            query_string='multipart-manifest=delete',
            response_dict={}
        )
        self._assertDictEqual(expected_r, r)

    def test_delete_object_dlo_support(self):
        mock_q = Queue()
        s = SwiftService()
        mock_conn = self._get_mock_connection()
        expected_r = self._get_expected({
            'action': 'delete_object',
            'success': True,
            'dlo_segments_deleted': True
        })
        # A DLO object is determined in _delete_object by heading the object
        # and checking for the existence of a x-object-manifest header.
        # Mock that here.
        mock_conn.head_object = Mock(
            return_value={'x-object-manifest': 'manifest_c/manifest_p'}
        )
        mock_conn.get_container = Mock(
            side_effect=[(None, [{'name': 'test_seg_1'},
                                 {'name': 'test_seg_2'}]),
                         (None, {})]
        )

        def get_mock_list_conn(options):
            return mock_conn

        with mock.patch('swiftclient.service.get_conn', get_mock_list_conn):
            r = s._delete_object(
                mock_conn, 'test_c', 'test_o', self.opts, mock_q
            )

        self._assertDictEqual(expected_r, r)
        expected = [
            mock.call('test_c', 'test_o', query_string=None, response_dict={}),
            mock.call('manifest_c', 'test_seg_1', response_dict={}),
            mock.call('manifest_c', 'test_seg_2', response_dict={})]
        mock_conn.delete_object.assert_has_calls(expected, any_order=True)

    def test_delete_empty_container(self):
        mock_conn = self._get_mock_connection()
        expected_r = self._get_expected({
            'action': 'delete_container',
            'success': True,
            'object': None
        })

        r = SwiftService._delete_empty_container(mock_conn, 'test_c')

        mock_conn.delete_container.assert_called_once_with(
            'test_c', response_dict={}
        )
        self._assertDictEqual(expected_r, r)

    def test_delete_empty_container_exception(self):
        mock_conn = self._get_mock_connection()
        mock_conn.delete_container = Mock(side_effect=self.exc)
        expected_r = self._get_expected({
            'action': 'delete_container',
            'success': False,
            'object': None,
            'error': self.exc,
            'traceback': mock.ANY,
            'error_timestamp': mock.ANY
        })

        before = time.time()
        s = SwiftService()
        r = s._delete_empty_container(mock_conn, 'test_c')
        after = time.time()

        mock_conn.delete_container.assert_called_once_with(
            'test_c', response_dict={}
        )
        self._assertDictEqual(expected_r, r)
        self.assertGreaterEqual(r['error_timestamp'], before)
        self.assertLessEqual(r['error_timestamp'], after)
        self.assertTrue('Traceback' in r['traceback'])


class TestSwiftError(testtools.TestCase):

    def test_is_exception(self):
        se = SwiftError(5)
        self.assertTrue(isinstance(se, Exception))

    def test_empty_swifterror_creation(self):
        se = SwiftError(5)

        self.assertEqual(se.value, 5)
        self.assertEqual(se.container, None)
        self.assertEqual(se.obj, None)
        self.assertEqual(se.segment, None)
        self.assertEqual(se.exception, None)

        self.assertEqual(str(se), '5')

    def test_swifterror_creation(self):
        test_exc = Exception('test exc')
        se = SwiftError(5, 'con', 'obj', 'seg', test_exc)

        self.assertEqual(se.value, 5)
        self.assertEqual(se.container, 'con')
        self.assertEqual(se.obj, 'obj')
        self.assertEqual(se.segment, 'seg')
        self.assertEqual(se.exception, test_exc)

        self.assertEqual(str(se), '5 container:con object:obj segment:seg')


class TestServiceUtils(testtools.TestCase):

    def setUp(self):
        super(TestServiceUtils, self).setUp()
        with mock.patch.dict(swiftclient.service.environ, clean_os_environ):
            swiftclient.service._default_global_options = \
                swiftclient.service._build_default_global_options()
        self.opts = swiftclient.service._default_global_options.copy()

    def test_process_options_defaults(self):
        # The only actions that should be taken on default options set is
        # to change the auth version to v2.0 and create the os_options dict
        opt_c = self.opts.copy()

        swiftclient.service.process_options(opt_c)

        self.assertTrue('os_options' in opt_c)
        del opt_c['os_options']
        self.assertEqual(opt_c['auth_version'], '2.0')
        opt_c['auth_version'] = '1.0'

        self.assertEqual(opt_c, self.opts)

    def test_process_options_auth_version(self):
        # auth_version should be set to 2.0
        # if it isnt already set to 3.0
        # and the v1 command line arguments arent present
        opt_c = self.opts.copy()

        # Check v3 isnt changed
        opt_c['auth_version'] = '3'
        swiftclient.service.process_options(opt_c)
        self.assertEqual(opt_c['auth_version'], '3')

        # Check v1 isnt changed if user, key and auth are set
        opt_c = self.opts.copy()
        opt_c['auth_version'] = '1'
        opt_c['auth'] = True
        opt_c['user'] = True
        opt_c['key'] = True
        swiftclient.service.process_options(opt_c)
        self.assertEqual(opt_c['auth_version'], '1')

    def test_process_options_new_style_args(self):
        # checks new style args are copied to old style
        # when old style dont exist
        opt_c = self.opts.copy()

        opt_c['auth'] = ''
        opt_c['user'] = ''
        opt_c['key'] = ''
        opt_c['os_auth_url'] = 'os_auth'
        opt_c['os_username'] = 'os_user'
        opt_c['os_password'] = 'os_pass'
        swiftclient.service.process_options(opt_c)
        self.assertEqual(opt_c['auth_version'], '2.0')
        self.assertEqual(opt_c['auth'], 'os_auth')
        self.assertEqual(opt_c['user'], 'os_user')
        self.assertEqual(opt_c['key'], 'os_pass')

        # Check old style args are left alone if they exist
        opt_c = self.opts.copy()
        opt_c['auth'] = 'auth'
        opt_c['user'] = 'user'
        opt_c['key'] = 'key'
        opt_c['os_auth_url'] = 'os_auth'
        opt_c['os_username'] = 'os_user'
        opt_c['os_password'] = 'os_pass'
        swiftclient.service.process_options(opt_c)
        self.assertEqual(opt_c['auth_version'], '1.0')
        self.assertEqual(opt_c['auth'], 'auth')
        self.assertEqual(opt_c['user'], 'user')
        self.assertEqual(opt_c['key'], 'key')

    def test_split_headers(self):
        mock_headers = ['color:blue', 'size:large']
        expected = {'Color': 'blue', 'Size': 'large'}

        actual = swiftclient.service.split_headers(mock_headers)
        self.assertEqual(expected, actual)

    def test_split_headers_prefix(self):
        mock_headers = ['color:blue', 'size:large']
        expected = {'Prefix-Color': 'blue', 'Prefix-Size': 'large'}

        actual = swiftclient.service.split_headers(mock_headers, 'prefix-')
        self.assertEqual(expected, actual)

    def test_split_headers_error(self):
        mock_headers = ['notvalid']

        self.assertRaises(SwiftError, swiftclient.service.split_headers,
                          mock_headers)


class TestSwiftUploadObject(testtools.TestCase):

    def setUp(self):
        self.suo = swiftclient.service.SwiftUploadObject
        super(TestSwiftUploadObject, self).setUp()

    def test_create_with_string(self):
        suo = self.suo('source')
        self.assertEqual(suo.source, 'source')
        self.assertEqual(suo.object_name, 'source')
        self.assertEqual(suo.options, None)

        suo = self.suo('source', 'obj_name')
        self.assertEqual(suo.source, 'source')
        self.assertEqual(suo.object_name, 'obj_name')
        self.assertEqual(suo.options, None)

        suo = self.suo('source', 'obj_name', {'opt': '123'})
        self.assertEqual(suo.source, 'source')
        self.assertEqual(suo.object_name, 'obj_name')
        self.assertEqual(suo.options, {'opt': '123'})

    def test_create_with_file(self):
        with tempfile.TemporaryFile() as mock_file:
            # Check error is raised if no object name is provided with a
            # filelike object
            self.assertRaises(SwiftError, self.suo, mock_file)

            # Check that empty strings are invalid object names
            self.assertRaises(SwiftError, self.suo, mock_file, '')

            suo = self.suo(mock_file, 'obj_name')
            self.assertEqual(suo.source, mock_file)
            self.assertEqual(suo.object_name, 'obj_name')
            self.assertEqual(suo.options, None)

            suo = self.suo(mock_file, 'obj_name', {'opt': '123'})
            self.assertEqual(suo.source, mock_file)
            self.assertEqual(suo.object_name, 'obj_name')
            self.assertEqual(suo.options, {'opt': '123'})

    def test_create_with_no_source(self):
        suo = self.suo(None, 'obj_name')
        self.assertEqual(suo.source, None)
        self.assertEqual(suo.object_name, 'obj_name')
        self.assertEqual(suo.options, None)

        # Check error is raised if source is None without an object name
        self.assertRaises(SwiftError, self.suo, None)

    def test_create_with_invalid_source(self):
        # Source can only be None, string or filelike object,
        # check an error is raised with an invalid type.
        self.assertRaises(SwiftError, self.suo, [])


class TestServiceList(_TestServiceBase):
    def setUp(self):
        super(TestServiceList, self).setUp()
        self.opts = {'prefix': None, 'long': False, 'delimiter': ''}
        self.exc = Exception('test_exc')
        # Base response to be copied and updated to matched the expected
        # response for each test
        self.expected = {
            'action': None,   # Should be list_X_part (account or container)
            'container': None,   # Should be a string when listing a container
            'prefix': None,
            'success': None   # Should be a bool
        }

    def test_list_account(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        get_account_returns = [
            (None, [{'name': 'test_c'}]),
            (None, [])
        ]
        mock_conn.get_account = Mock(side_effect=get_account_returns)

        expected_r = self._get_expected({
            'action': 'list_account_part',
            'success': True,
            'listing': [{'name': 'test_c'}],
            'marker': ''
        })

        SwiftService._list_account_job(
            mock_conn, self.opts, mock_q
        )
        self._assertDictEqual(expected_r, self._get_queue(mock_q))
        self.assertIsNone(self._get_queue(mock_q))

        long_opts = dict(self.opts, **{'long': True})
        mock_conn.head_container = Mock(return_value={'test_m': '1'})
        get_account_returns = [
            (None, [{'name': 'test_c'}]),
            (None, [])
        ]
        mock_conn.get_account = Mock(side_effect=get_account_returns)

        expected_r_long = self._get_expected({
            'action': 'list_account_part',
            'success': True,
            'listing': [{'name': 'test_c', 'meta': {'test_m': '1'}}],
            'marker': '',
        })

        SwiftService._list_account_job(
            mock_conn, long_opts, mock_q
        )
        self._assertDictEqual(expected_r_long, self._get_queue(mock_q))
        self.assertIsNone(self._get_queue(mock_q))

    def test_list_account_exception(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        mock_conn.get_account = Mock(side_effect=self.exc)
        expected_r = self._get_expected({
            'action': 'list_account_part',
            'success': False,
            'error': self.exc,
            'marker': '',
            'traceback': mock.ANY,
            'error_timestamp': mock.ANY
        })

        SwiftService._list_account_job(
            mock_conn, self.opts, mock_q)

        mock_conn.get_account.assert_called_once_with(
            marker='', prefix=None
        )
        self._assertDictEqual(expected_r, self._get_queue(mock_q))
        self.assertIsNone(self._get_queue(mock_q))

    def test_list_container(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        get_container_returns = [
            (None, [{'name': 'test_o'}]),
            (None, [])
        ]
        mock_conn.get_container = Mock(side_effect=get_container_returns)

        expected_r = self._get_expected({
            'action': 'list_container_part',
            'container': 'test_c',
            'success': True,
            'listing': [{'name': 'test_o'}],
            'marker': ''
        })

        SwiftService._list_container_job(
            mock_conn, 'test_c', self.opts, mock_q
        )
        self._assertDictEqual(expected_r, self._get_queue(mock_q))
        self.assertIsNone(self._get_queue(mock_q))

        long_opts = dict(self.opts, **{'long': True})
        mock_conn.head_container = Mock(return_value={'test_m': '1'})
        get_container_returns = [
            (None, [{'name': 'test_o'}]),
            (None, [])
        ]
        mock_conn.get_container = Mock(side_effect=get_container_returns)

        expected_r_long = self._get_expected({
            'action': 'list_container_part',
            'container': 'test_c',
            'success': True,
            'listing': [{'name': 'test_o'}],
            'marker': ''
        })

        SwiftService._list_container_job(
            mock_conn, 'test_c', long_opts, mock_q
        )
        self._assertDictEqual(expected_r_long, self._get_queue(mock_q))
        self.assertIsNone(self._get_queue(mock_q))

    def test_list_container_exception(self):
        mock_q = Queue()
        mock_conn = self._get_mock_connection()
        mock_conn.get_container = Mock(side_effect=self.exc)
        expected_r = self._get_expected({
            'action': 'list_container_part',
            'container': 'test_c',
            'success': False,
            'error': self.exc,
            'marker': '',
            'error_timestamp': mock.ANY,
            'traceback': mock.ANY
        })

        SwiftService._list_container_job(
            mock_conn, 'test_c', self.opts, mock_q
        )

        mock_conn.get_container.assert_called_once_with(
            'test_c', marker='', delimiter='', prefix=None
        )
        self._assertDictEqual(expected_r, self._get_queue(mock_q))
        self.assertIsNone(self._get_queue(mock_q))

    @mock.patch('swiftclient.service.get_conn')
    def test_list_queue_size(self, mock_get_conn):
        mock_conn = self._get_mock_connection()
        # Return more results than should fit in the results queue
        get_account_returns = [
            (None, [{'name': 'container1'}]),
            (None, [{'name': 'container2'}]),
            (None, [{'name': 'container3'}]),
            (None, [{'name': 'container4'}]),
            (None, [{'name': 'container5'}]),
            (None, [{'name': 'container6'}]),
            (None, [{'name': 'container7'}]),
            (None, [{'name': 'container8'}]),
            (None, [{'name': 'container9'}]),
            (None, [{'name': 'container10'}]),
            (None, [{'name': 'container11'}]),
            (None, [{'name': 'container12'}]),
            (None, [{'name': 'container13'}]),
            (None, [{'name': 'container14'}]),
            (None, [])
        ]
        mock_conn.get_account = Mock(side_effect=get_account_returns)
        mock_get_conn.return_value = mock_conn

        s = SwiftService(options=self.opts)
        lg = s.list()

        # Start the generator
        first_list_part = next(lg)

        # Wait for the number of calls to get_account to reach our expected
        # value, then let it run some more to make sure the value remains
        # stable
        count = mock_conn.get_account.call_count
        stable = 0
        while mock_conn.get_account.call_count != count or stable < 5:
            if mock_conn.get_account.call_count == count:
                stable += 1
            else:
                count = mock_conn.get_account.call_count
                stable = 0
            # The test requires a small sleep to allow other threads to
            # execute - in this mocked environment we assume that if the call
            # count to get_account has not changed in 0.25s then no more calls
            # will be made.
            sleep(0.05)

        stable_get_account_call_count = mock_conn.get_account.call_count

        # Collect all remaining results from the generator
        list_results = [first_list_part] + list(lg)

        # Make sure the stable call count is correct - this should be 12 calls
        # to get_account;
        #  1 for first_list_part
        #  10 for the values on the queue
        #  1 for the value blocking whilst trying to place onto the queue
        self.assertEqual(12, stable_get_account_call_count)

        # Make sure all the containers were listed and placed onto the queue
        self.assertEqual(15, mock_conn.get_account.call_count)

        # Check the results were all returned
        observed_listing = []
        for lir in list_results:
            observed_listing.append(
                [li['name'] for li in lir['listing']]
            )
        expected_listing = []
        for gar in get_account_returns[:-1]:  # The empty list is not returned
            expected_listing.append(
                [li['name'] for li in gar[1]]
            )
        self.assertEqual(observed_listing, expected_listing)


class TestService(testtools.TestCase):

    def test_upload_with_bad_segment_size(self):
        for bad in ('ten', '1234X', '100.3'):
            options = {'segment_size': bad}
            try:
                service = SwiftService(options)
                next(service.upload('c', 'o'))
                self.fail('Expected SwiftError when segment_size=%s' % bad)
            except SwiftError as exc:
                self.assertEqual('Segment size should be an integer value',
                                 exc.value)

    @mock.patch('swiftclient.service.stat')
    @mock.patch('swiftclient.service.getmtime', return_value=1.0)
    @mock.patch('swiftclient.service.getsize', return_value=4)
    @mock.patch.object(builtins, 'open', return_value=six.StringIO('asdf'))
    def test_upload_with_relative_path(self, *args, **kwargs):
        service = SwiftService({})
        objects = [{'path': "./test",
                    'strt_indx': 2},
                   {'path': os.path.join(os.getcwd(), "test"),
                    'strt_indx': 1},
                   {'path': ".\\test",
                    'strt_indx': 2}]
        for obj in objects:
            with mock.patch('swiftclient.service.Connection') as mock_conn:
                mock_conn.return_value.head_object.side_effect = \
                    ClientException('Not Found', http_status=404)
                mock_conn.return_value.put_object.return_value =\
                    'd41d8cd98f00b204e9800998ecf8427e'
                resp_iter = service.upload(
                    'c', [SwiftUploadObject(obj['path'])])
                responses = [x for x in resp_iter]
                for resp in responses:
                    self.assertTrue(resp['success'])
                self.assertEqual(2, len(responses))
                create_container_resp, upload_obj_resp = responses
                self.assertEqual(create_container_resp['action'],
                                 'create_container')
                self.assertEqual(upload_obj_resp['action'],
                                 'upload_object')
                self.assertEqual(upload_obj_resp['object'],
                                 obj['path'][obj['strt_indx']:])
                self.assertEqual(upload_obj_resp['path'], obj['path'])


class TestServiceUpload(_TestServiceBase):

    def test_upload_segment_job(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 10)
            f.write(b'b' * 10)
            f.write(b'c' * 10)
            f.flush()

            # Mock the connection to return an empty etag. This
            # skips etag validation which would fail as the LengthWrapper
            # isnt read from.
            mock_conn = mock.Mock()
            mock_conn.put_object.return_value = ''
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)
            expected_r = {
                'action': 'upload_segment',
                'for_object': 'test_o',
                'segment_index': 2,
                'segment_size': 10,
                'segment_location': '/test_c_segments/test_s_1',
                'log_line': 'test_o segment 2',
                'success': True,
                'response_dict': {},
                'segment_etag': '',
                'attempts': 2,
            }

            s = SwiftService()
            r = s._upload_segment_job(conn=mock_conn,
                                      path=f.name,
                                      container='test_c',
                                      segment_name='test_s_1',
                                      segment_start=10,
                                      segment_size=10,
                                      segment_index=2,
                                      obj_name='test_o',
                                      options={'segment_container': None,
                                               'checksum': True})

            self._assertDictEqual(r, expected_r)

            self.assertEqual(mock_conn.put_object.call_count, 1)
            mock_conn.put_object.assert_called_with('test_c_segments',
                                                    'test_s_1',
                                                    mock.ANY,
                                                    content_length=10,
                                                    response_dict={})
            contents = mock_conn.put_object.call_args[0][2]
            self.assertIsInstance(contents, utils.LengthWrapper)
            self.assertEqual(len(contents), 10)
            # This read forces the LengthWrapper to calculate the md5
            # for the read content.
            self.assertEqual(contents.read(), b'b' * 10)
            self.assertEqual(contents.get_md5sum(), md5(b'b' * 10).hexdigest())

    def test_etag_mismatch_with_ignore_checksum(self):
        def _consuming_conn(*a, **kw):
            contents = a[2]
            contents.read()  # Force md5 calculation
            return 'badresponseetag'

        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 10)
            f.write(b'b' * 10)
            f.write(b'c' * 10)
            f.flush()

            mock_conn = mock.Mock()
            mock_conn.put_object.side_effect = _consuming_conn
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_segment_job(conn=mock_conn,
                                      path=f.name,
                                      container='test_c',
                                      segment_name='test_s_1',
                                      segment_start=10,
                                      segment_size=10,
                                      segment_index=2,
                                      obj_name='test_o',
                                      options={'segment_container': None,
                                               'checksum': False})

            self.assertNotIn('error', r)
            self.assertEqual(mock_conn.put_object.call_count, 1)
            mock_conn.put_object.assert_called_with('test_c_segments',
                                                    'test_s_1',
                                                    mock.ANY,
                                                    content_length=10,
                                                    response_dict={})
            contents = mock_conn.put_object.call_args[0][2]
            # Check that md5sum is not calculated.
            self.assertEqual(contents.get_md5sum(), '')

    def test_upload_segment_job_etag_mismatch(self):
        def _consuming_conn(*a, **kw):
            contents = a[2]
            contents.read()  # Force md5 calculation
            return 'badresponseetag'

        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 10)
            f.write(b'b' * 10)
            f.write(b'c' * 10)
            f.flush()

            mock_conn = mock.Mock()
            mock_conn.put_object.side_effect = _consuming_conn
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_segment_job(conn=mock_conn,
                                      path=f.name,
                                      container='test_c',
                                      segment_name='test_s_1',
                                      segment_start=10,
                                      segment_size=10,
                                      segment_index=2,
                                      obj_name='test_o',
                                      options={'segment_container': None,
                                               'checksum': True})

            self.assertIn('error', r)
            self.assertIn('md5 mismatch', str(r['error']))

            self.assertEqual(mock_conn.put_object.call_count, 1)
            mock_conn.put_object.assert_called_with('test_c_segments',
                                                    'test_s_1',
                                                    mock.ANY,
                                                    content_length=10,
                                                    response_dict={})
            contents = mock_conn.put_object.call_args[0][2]
            self.assertEqual(contents.get_md5sum(), md5(b'b' * 10).hexdigest())

    def test_upload_object_job_file(self):
        # Uploading a file results in the file object being wrapped in a
        # LengthWrapper. This test sets the options in such a way that much
        # of _upload_object_job is skipped bringing the critical path down
        # to around 60 lines to ease testing.
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()
            expected_r = {
                'action': 'upload_object',
                'attempts': 2,
                'container': 'test_c',
                'headers': {},
                'large_object': False,
                'object': 'test_o',
                'response_dict': {},
                'status': 'uploaded',
                'success': True,
            }
            expected_mtime = float(os.path.getmtime(f.name))

            mock_conn = mock.Mock()
            mock_conn.put_object.return_value = ''
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_object_job(conn=mock_conn,
                                     container='test_c',
                                     source=f.name,
                                     obj='test_o',
                                     options={'changed': False,
                                              'skip_identical': False,
                                              'leave_segments': True,
                                              'header': '',
                                              'segment_size': 0,
                                              'checksum': True})

            mtime = float(r['headers']['x-object-meta-mtime'])
            self.assertAlmostEqual(mtime, expected_mtime, delta=0.5)
            del r['headers']['x-object-meta-mtime']

            self.assertEqual(r['path'], f.name)
            del r['path']

            self._assertDictEqual(r, expected_r)
            self.assertEqual(mock_conn.put_object.call_count, 1)
            mock_conn.put_object.assert_called_with('test_c', 'test_o',
                                                    mock.ANY,
                                                    content_length=30,
                                                    headers={},
                                                    response_dict={})
            contents = mock_conn.put_object.call_args[0][2]
            self.assertIsInstance(contents, utils.LengthWrapper)
            self.assertEqual(len(contents), 30)
            # This read forces the LengthWrapper to calculate the md5
            # for the read content. This also checks that LengthWrapper was
            # initialized with md5=True
            self.assertEqual(contents.read(), b'a' * 30)
            self.assertEqual(contents.get_md5sum(), md5(b'a' * 30).hexdigest())

    def test_upload_object_job_stream(self):
        # Streams are wrapped as ReadableToIterable
        with tempfile.TemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()
            f.seek(0)
            expected_r = {
                'action': 'upload_object',
                'attempts': 2,
                'container': 'test_c',
                'headers': {},
                'large_object': False,
                'object': 'test_o',
                'response_dict': {},
                'status': 'uploaded',
                'success': True,
                'path': None,
            }
            expected_mtime = float(time.time())

            mock_conn = mock.Mock()
            mock_conn.put_object.return_value = ''
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_object_job(conn=mock_conn,
                                     container='test_c',
                                     source=f,
                                     obj='test_o',
                                     options={'changed': False,
                                              'skip_identical': False,
                                              'leave_segments': True,
                                              'header': '',
                                              'segment_size': 0,
                                              'checksum': True})

            mtime = float(r['headers']['x-object-meta-mtime'])
            self.assertAlmostEqual(mtime, expected_mtime, delta=0.5)
            del r['headers']['x-object-meta-mtime']

            self._assertDictEqual(r, expected_r)
            self.assertEqual(mock_conn.put_object.call_count, 1)
            mock_conn.put_object.assert_called_with('test_c', 'test_o',
                                                    mock.ANY,
                                                    content_length=None,
                                                    headers={},
                                                    response_dict={})
            contents = mock_conn.put_object.call_args[0][2]
            self.assertIsInstance(contents, utils.ReadableToIterable)
            self.assertEqual(contents.chunk_size, 65536)
            # next retrieves the first chunk of the stream or len(chunk_size)
            # or less, it also forces the md5 to be calculated.
            self.assertEqual(next(contents), b'a' * 30)
            self.assertEqual(contents.get_md5sum(), md5(b'a' * 30).hexdigest())

    def test_upload_object_job_etag_mismatch(self):
        # The etag test for both streams and files use the same code
        # so only one test should be needed.
        def _consuming_conn(*a, **kw):
            contents = a[2]
            contents.read()  # Force md5 calculation
            return 'badresponseetag'

        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()

            mock_conn = mock.Mock()
            mock_conn.put_object.side_effect = _consuming_conn
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_object_job(conn=mock_conn,
                                     container='test_c',
                                     source=f.name,
                                     obj='test_o',
                                     options={'changed': False,
                                              'skip_identical': False,
                                              'leave_segments': True,
                                              'header': '',
                                              'segment_size': 0,
                                              'checksum': True})

            self.assertEqual(r['success'], False)
            self.assertIn('error', r)
            self.assertIn('md5 mismatch', str(r['error']))

            self.assertEqual(mock_conn.put_object.call_count, 1)
            expected_headers = {'x-object-meta-mtime': mock.ANY}
            mock_conn.put_object.assert_called_with('test_c', 'test_o',
                                                    mock.ANY,
                                                    content_length=30,
                                                    headers=expected_headers,
                                                    response_dict={})

            contents = mock_conn.put_object.call_args[0][2]
            self.assertEqual(contents.get_md5sum(), md5(b'a' * 30).hexdigest())

    def test_upload_object_job_identical_etag(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()

            mock_conn = mock.Mock()
            mock_conn.head_object.return_value = {
                'content-length': 30,
                'etag': md5(b'a' * 30).hexdigest()}
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_object_job(conn=mock_conn,
                                     container='test_c',
                                     source=f.name,
                                     obj='test_o',
                                     options={'changed': False,
                                              'skip_identical': True,
                                              'leave_segments': True,
                                              'header': '',
                                              'segment_size': 0})

            self.assertTrue(r['success'])
            self.assertIn('status', r)
            self.assertEqual(r['status'], 'skipped-identical')
            self.assertEqual(mock_conn.put_object.call_count, 0)
            self.assertEqual(mock_conn.head_object.call_count, 1)
            mock_conn.head_object.assert_called_with('test_c', 'test_o')

    def test_upload_object_job_identical_slo_with_nesting(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()
            seg_etag = md5(b'a' * 10).hexdigest()
            submanifest = "[%s]" % ",".join(
                ['{"bytes":10,"hash":"%s"}' % seg_etag] * 2)
            submanifest_etag = md5(seg_etag.encode('ascii') * 2).hexdigest()
            manifest = "[%s]" % ",".join([
                '{"sub_slo":true,"name":"/test_c_segments/test_sub_slo",'
                '"bytes":20,"hash":"%s"}' % submanifest_etag,
                '{"bytes":10,"hash":"%s"}' % seg_etag])

            mock_conn = mock.Mock()
            mock_conn.head_object.return_value = {
                'x-static-large-object': True,
                'content-length': 30,
                'etag': md5(submanifest_etag.encode('ascii') +
                            seg_etag.encode('ascii')).hexdigest()}
            mock_conn.get_object.side_effect = [
                ({}, manifest.encode('ascii')),
                ({}, submanifest.encode('ascii'))]
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            r = s._upload_object_job(conn=mock_conn,
                                     container='test_c',
                                     source=f.name,
                                     obj='test_o',
                                     options={'changed': False,
                                              'skip_identical': True,
                                              'leave_segments': True,
                                              'header': '',
                                              'segment_size': 10})

            self.assertIsNone(r.get('error'))
            self.assertTrue(r['success'])
            self.assertEqual('skipped-identical', r.get('status'))
            self.assertEqual(0, mock_conn.put_object.call_count)
            self.assertEqual([mock.call('test_c', 'test_o')],
                             mock_conn.head_object.mock_calls)
            self.assertEqual([
                mock.call('test_c', 'test_o',
                          query_string='multipart-manifest=get'),
                mock.call('test_c_segments', 'test_sub_slo',
                          query_string='multipart-manifest=get'),
            ], mock_conn.get_object.mock_calls)

    def test_upload_object_job_identical_dlo(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()
            segment_etag = md5(b'a' * 10).hexdigest()

            mock_conn = mock.Mock()
            mock_conn.head_object.return_value = {
                'x-object-manifest': 'test_c_segments/test_o/prefix',
                'content-length': 30,
                'etag': md5(segment_etag.encode('ascii') * 3).hexdigest()}
            mock_conn.get_container.side_effect = [
                (None, [{"bytes": 10, "hash": segment_etag,
                         "name": "test_o/prefix/00"},
                        {"bytes": 10, "hash": segment_etag,
                         "name": "test_o/prefix/01"}]),
                (None, [{"bytes": 10, "hash": segment_etag,
                         "name": "test_o/prefix/02"}]),
                (None, {})]
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)

            s = SwiftService()
            with mock.patch('swiftclient.service.get_conn',
                            return_value=mock_conn):
                r = s._upload_object_job(conn=mock_conn,
                                         container='test_c',
                                         source=f.name,
                                         obj='test_o',
                                         options={'changed': False,
                                                  'skip_identical': True,
                                                  'leave_segments': True,
                                                  'header': '',
                                                  'segment_size': 10})

            self.assertIsNone(r.get('error'))
            self.assertTrue(r['success'])
            self.assertEqual('skipped-identical', r.get('status'))
            self.assertEqual(0, mock_conn.put_object.call_count)
            self.assertEqual(1, mock_conn.head_object.call_count)
            self.assertEqual(3, mock_conn.get_container.call_count)
            mock_conn.head_object.assert_called_with('test_c', 'test_o')
            expected = [
                mock.call('test_c_segments', prefix='test_o/prefix',
                          marker='', delimiter=None),
                mock.call('test_c_segments', prefix='test_o/prefix',
                          marker="test_o/prefix/01", delimiter=None),
                mock.call('test_c_segments', prefix='test_o/prefix',
                          marker="test_o/prefix/02", delimiter=None),
            ]
            mock_conn.get_container.assert_has_calls(expected)


class TestServiceDownload(_TestServiceBase):

    def setUp(self):
        super(TestServiceDownload, self).setUp()
        self.opts = swiftclient.service._default_local_options.copy()
        self.opts['no_download'] = True
        self.obj_content = b'c' * 10
        self.obj_etag = md5(self.obj_content).hexdigest()
        self.obj_len = len(self.obj_content)
        self.exc = Exception('test_exc')
        # Base response to be copied and updated to matched the expected
        # response for each test
        self.expected = {
            'action': 'download_object',   # Should always be download_object
            'container': 'test_c',
            'object': 'test_o',
            'attempts': 2,
            'response_dict': {},
            'path': 'test_o',
            'pseudodir': False,
            'success': None   # Should be a bool
        }

    def _readbody(self):
        yield self.obj_content

    def _assertDictEqual(self, a, b, m=None):
        # assertDictEqual is not available in py2.6 so use a shallow check
        # instead
        if not m:
            m = '{0} != {1}'.format(a, b)

        if hasattr(self, 'assertDictEqual'):
            self.assertDictEqual(a, b, m)
        else:
            self.assertTrue(isinstance(a, dict), m)
            self.assertTrue(isinstance(b, dict), m)
            self.assertEqual(len(a), len(b), m)
            for k, v in a.items():
                self.assertIn(k, b, m)
                self.assertEqual(b[k], v, m)

    @mock.patch('swiftclient.service.SwiftService.list')
    @mock.patch('swiftclient.service.SwiftService._submit_page_downloads')
    @mock.patch('swiftclient.service.interruptable_as_completed')
    def test_download_container_job(self, as_comp, sub_page, service_list):
        """
        Check that paged downloads work correctly
        """
        as_comp.side_effect = [

        ]
        sub_page.side_effect = [
            range(0, 10), range(0, 10), []  # simulate multiple result pages
        ]
        r = Mock(spec=Future)
        r.result.return_value = self._get_expected({
            'success': True,
            'start_time': 1,
            'finish_time': 2,
            'headers_receipt': 3,
            'auth_end_time': 4,
            'read_length': len(b'objcontent'),
        })
        as_comp.side_effect = [
            [r for _ in range(0, 10)],
            [r for _ in range(0, 10)]
        ]

        s = SwiftService()
        down_gen = s._download_container('test_c', self.opts)
        results = list(down_gen)
        self.assertEqual(20, len(results))

    @mock.patch('swiftclient.service.SwiftService.list')
    @mock.patch('swiftclient.service.SwiftService._submit_page_downloads')
    @mock.patch('swiftclient.service.interruptable_as_completed')
    def test_download_container_job_error(
            self, as_comp, sub_page, service_list):
        """
        Check that paged downloads work correctly
        """
        class BoomError(Exception):
            def __init__(self, value):
                self.value = value

            def __str__(self):
                return repr(self.value)

        def _make_result():
            r = Mock(spec=Future)
            r.result.return_value = self._get_expected({
                'success': True,
                'start_time': 1,
                'finish_time': 2,
                'headers_receipt': 3,
                'auth_end_time': 4,
                'read_length': len(b'objcontent'),
            })
            return r

        as_comp.side_effect = [

        ]
        # We need Futures here because the error will cause a call to .cancel()
        sub_page_effects = [
            [_make_result() for _ in range(0, 10)],
            BoomError('Go Boom')
        ]
        sub_page.side_effect = sub_page_effects
        # ...but we must also mock the returns to as_completed
        as_comp.side_effect = [
            [_make_result() for _ in range(0, 10)]
        ]

        s = SwiftService()
        self.assertRaises(
            BoomError,
            lambda: list(s._download_container('test_c', self.opts))
        )
        # This was an unknown error, so make sure we attempt to cancel futures
        for spe in sub_page_effects[0]:
            spe.cancel.assert_called_once_with()

        # Now test ClientException
        sub_page_effects = [
            [_make_result() for _ in range(0, 10)],
            ClientException('Go Boom')
        ]
        sub_page.side_effect = sub_page_effects
        as_comp.side_effect = [
            [_make_result() for _ in range(0, 10)],
            [_make_result() for _ in range(0, 10)]
        ]
        self.assertRaises(
            ClientException,
            lambda: list(s._download_container('test_c', self.opts))
        )
        # This was a ClientException, so make sure we don't cancel futures
        for spe in sub_page_effects[0]:
            self.assertFalse(spe.cancel.called)

    def test_download_object_job(self):
        mock_conn = self._get_mock_connection()
        objcontent = six.BytesIO(b'objcontent')
        mock_conn.get_object.side_effect = [
            ({'content-type': 'text/plain',
              'etag': '2cbbfe139a744d6abbe695e17f3c1991'},
             objcontent)
        ]
        expected_r = self._get_expected({
            'success': True,
            'start_time': 1,
            'finish_time': 2,
            'headers_receipt': 3,
            'auth_end_time': 4,
            'read_length': len(b'objcontent'),
        })

        with mock.patch.object(builtins, 'open') as mock_open:
            written_content = Mock()
            mock_open.return_value = written_content
            s = SwiftService()
            _opts = self.opts.copy()
            _opts['no_download'] = False
            actual_r = s._download_object_job(
                mock_conn, 'test_c', 'test_o', _opts)
            actual_r = dict(  # Need to override the times we got from the call
                actual_r,
                **{
                    'start_time': 1,
                    'finish_time': 2,
                    'headers_receipt': 3
                }
            )
            mock_open.assert_called_once_with('test_o', 'wb')
            written_content.write.assert_called_once_with(b'objcontent')

        mock_conn.get_object.assert_called_once_with(
            'test_c', 'test_o', resp_chunk_size=65536, headers={},
            response_dict={}
        )
        self._assertDictEqual(expected_r, actual_r)

    def test_download_object_job_exception(self):
        mock_conn = self._get_mock_connection()
        mock_conn.get_object = Mock(side_effect=self.exc)
        expected_r = self._get_expected({
            'success': False,
            'error': self.exc,
            'error_timestamp': mock.ANY,
            'traceback': mock.ANY
        })

        s = SwiftService()
        actual_r = s._download_object_job(
            mock_conn, 'test_c', 'test_o', self.opts)

        mock_conn.get_object.assert_called_once_with(
            'test_c', 'test_o', resp_chunk_size=65536, headers={},
            response_dict={}
        )
        self._assertDictEqual(expected_r, actual_r)

    def test_download(self):
        service = SwiftService()
        with mock.patch('swiftclient.service.Connection') as mock_conn:
            header = {'content-length': self.obj_len,
                      'etag': self.obj_etag}
            mock_conn.get_object.return_value = header, self._readbody()

            resp = service._download_object_job(mock_conn,
                                                'c',
                                                'test',
                                                self.opts)

        self.assertTrue(resp['success'])
        self.assertEqual(resp['action'], 'download_object')
        self.assertEqual(resp['object'], 'test')
        self.assertEqual(resp['path'], 'test')

    def test_download_with_output_dir(self):
        service = SwiftService()
        with mock.patch('swiftclient.service.Connection') as mock_conn:
            header = {'content-length': self.obj_len,
                      'etag': self.obj_etag}
            mock_conn.get_object.return_value = header, self._readbody()

            options = self.opts.copy()
            options['out_directory'] = 'temp_dir'
            resp = service._download_object_job(mock_conn,
                                                'c',
                                                'example/test',
                                                options)

        self.assertTrue(resp['success'])
        self.assertEqual(resp['action'], 'download_object')
        self.assertEqual(resp['object'], 'example/test')
        self.assertEqual(resp['path'], 'temp_dir/example/test')

    def test_download_with_remove_prefix(self):
        service = SwiftService()
        with mock.patch('swiftclient.service.Connection') as mock_conn:
            header = {'content-length': self.obj_len,
                      'etag': self.obj_etag}
            mock_conn.get_object.return_value = header, self._readbody()

            options = self.opts.copy()
            options['prefix'] = 'example/'
            options['remove_prefix'] = True
            resp = service._download_object_job(mock_conn,
                                                'c',
                                                'example/test',
                                                options)

        self.assertTrue(resp['success'])
        self.assertEqual(resp['action'], 'download_object')
        self.assertEqual(resp['object'], 'example/test')
        self.assertEqual(resp['path'], 'test')

    def test_download_with_remove_prefix_and_remove_slashes(self):
        service = SwiftService()
        with mock.patch('swiftclient.service.Connection') as mock_conn:
            header = {'content-length': self.obj_len,
                      'etag': self.obj_etag}
            mock_conn.get_object.return_value = header, self._readbody()

            options = self.opts.copy()
            options['prefix'] = 'example'
            options['remove_prefix'] = True
            resp = service._download_object_job(mock_conn,
                                                'c',
                                                'example/test',
                                                options)

        self.assertTrue(resp['success'])
        self.assertEqual(resp['action'], 'download_object')
        self.assertEqual(resp['object'], 'example/test')
        self.assertEqual(resp['path'], 'test')

    def test_download_with_output_dir_and_remove_prefix(self):
        service = SwiftService()
        with mock.patch('swiftclient.service.Connection') as mock_conn:
            header = {'content-length': self.obj_len,
                      'etag': self.obj_etag}
            mock_conn.get_object.return_value = header, self._readbody()

            options = self.opts.copy()
            options['prefix'] = 'example'
            options['out_directory'] = 'new/dir'
            options['remove_prefix'] = True
            resp = service._download_object_job(mock_conn,
                                                'c',
                                                'example/test',
                                                options)

        self.assertTrue(resp['success'])
        self.assertEqual(resp['action'], 'download_object')
        self.assertEqual(resp['object'], 'example/test')
        self.assertEqual(resp['path'], 'new/dir/test')

    def test_download_object_job_skip_identical(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()

            err = swiftclient.ClientException('Object GET failed',
                                              http_status=304)

            def fake_get(*args, **kwargs):
                kwargs['response_dict']['headers'] = {}
                raise err

            mock_conn = mock.Mock()
            mock_conn.get_object.side_effect = fake_get
            type(mock_conn).attempts = mock.PropertyMock(return_value=2)
            expected_r = {
                'action': 'download_object',
                'container': 'test_c',
                'object': 'test_o',
                'success': False,
                'error': err,
                'response_dict': {'headers': {}},
                'path': 'test_o',
                'pseudodir': False,
                'attempts': 2,
                'traceback': mock.ANY,
                'error_timestamp': mock.ANY
            }

            s = SwiftService()
            r = s._download_object_job(conn=mock_conn,
                                       container='test_c',
                                       obj='test_o',
                                       options={'out_file': f.name,
                                                'out_directory': None,
                                                'prefix': None,
                                                'remove_prefix': False,
                                                'header': {},
                                                'yes_all': False,
                                                'skip_identical': True})
            self._assertDictEqual(r, expected_r)

            self.assertEqual(mock_conn.get_object.call_count, 1)
            mock_conn.get_object.assert_called_with(
                'test_c',
                'test_o',
                resp_chunk_size=65536,
                headers={'If-None-Match': md5(b'a' * 30).hexdigest()},
                query_string='multipart-manifest=get',
                response_dict=expected_r['response_dict'])

    def test_download_object_job_skip_identical_dlo(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()
            on_disk_md5 = md5(b'a' * 30).hexdigest()
            segment_md5 = md5(b'a' * 10).hexdigest()

            mock_conn = mock.Mock()
            mock_conn.get_object.return_value = (
                {'x-object-manifest': 'test_c_segments/test_o/prefix'}, [b''])
            mock_conn.get_container.side_effect = [
                (None, [{'name': 'test_o/prefix/1',
                         'bytes': 10, 'hash': segment_md5},
                        {'name': 'test_o/prefix/2',
                         'bytes': 10, 'hash': segment_md5}]),
                (None, [{'name': 'test_o/prefix/3',
                         'bytes': 10, 'hash': segment_md5}]),
                (None, [])]

            type(mock_conn).attempts = mock.PropertyMock(return_value=2)
            expected_r = {
                'action': 'download_object',
                'container': 'test_c',
                'object': 'test_o',
                'success': False,
                'response_dict': {},
                'path': 'test_o',
                'pseudodir': False,
                'attempts': 2,
                'traceback': mock.ANY,
                'error_timestamp': mock.ANY
            }

            s = SwiftService()
            with mock.patch('swiftclient.service.get_conn',
                            return_value=mock_conn):
                r = s._download_object_job(conn=mock_conn,
                                           container='test_c',
                                           obj='test_o',
                                           options={'out_file': f.name,
                                                    'out_directory': None,
                                                    'prefix': None,
                                                    'remove_prefix': False,
                                                    'header': {},
                                                    'yes_all': False,
                                                    'skip_identical': True})

            err = r.pop('error')
            self.assertEqual("Large object is identical", err.msg)
            self.assertEqual(304, err.http_status)

            self._assertDictEqual(r, expected_r)

            self.assertEqual(mock_conn.get_object.call_count, 1)
            mock_conn.get_object.assert_called_with(
                'test_c',
                'test_o',
                resp_chunk_size=65536,
                headers={'If-None-Match': on_disk_md5},
                query_string='multipart-manifest=get',
                response_dict=expected_r['response_dict'])
            self.assertEqual(mock_conn.get_container.mock_calls, [
                mock.call('test_c_segments',
                          delimiter=None,
                          prefix='test_o/prefix',
                          marker=''),
                mock.call('test_c_segments',
                          delimiter=None,
                          prefix='test_o/prefix',
                          marker='test_o/prefix/2'),
                mock.call('test_c_segments',
                          delimiter=None,
                          prefix='test_o/prefix',
                          marker='test_o/prefix/3')])

    def test_download_object_job_skip_identical_nested_slo(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.flush()
            on_disk_md5 = md5(b'a' * 30).hexdigest()

            seg_etag = md5(b'a' * 10).hexdigest()
            submanifest = "[%s]" % ",".join(
                ['{"bytes":10,"hash":"%s"}' % seg_etag] * 2)
            submanifest_etag = md5(seg_etag.encode('ascii') * 2).hexdigest()
            manifest = "[%s]" % ",".join([
                '{"sub_slo":true,"name":"/test_c_segments/test_sub_slo",'
                '"bytes":20,"hash":"%s"}' % submanifest_etag,
                '{"bytes":10,"hash":"%s"}' % seg_etag])

            mock_conn = mock.Mock()
            mock_conn.get_object.side_effect = [
                ({'x-static-large-object': True,
                  'content-length': 30,
                  'etag': md5(submanifest_etag.encode('ascii') +
                              seg_etag.encode('ascii')).hexdigest()},
                 [manifest.encode('ascii')]),
                ({'x-static-large-object': True,
                  'content-length': 20,
                  'etag': submanifest_etag},
                 submanifest.encode('ascii'))]

            type(mock_conn).attempts = mock.PropertyMock(return_value=2)
            expected_r = {
                'action': 'download_object',
                'container': 'test_c',
                'object': 'test_o',
                'success': False,
                'response_dict': {},
                'path': 'test_o',
                'pseudodir': False,
                'attempts': 2,
                'traceback': mock.ANY,
                'error_timestamp': mock.ANY
            }

            s = SwiftService()
            with mock.patch('swiftclient.service.get_conn',
                            return_value=mock_conn):
                r = s._download_object_job(conn=mock_conn,
                                           container='test_c',
                                           obj='test_o',
                                           options={'out_file': f.name,
                                                    'out_directory': None,
                                                    'prefix': None,
                                                    'remove_prefix': False,
                                                    'header': {},
                                                    'yes_all': False,
                                                    'skip_identical': True})

            err = r.pop('error')
            self.assertEqual("Large object is identical", err.msg)
            self.assertEqual(304, err.http_status)

            self._assertDictEqual(r, expected_r)
            self.assertEqual(mock_conn.get_object.mock_calls, [
                mock.call('test_c',
                          'test_o',
                          resp_chunk_size=65536,
                          headers={'If-None-Match': on_disk_md5},
                          query_string='multipart-manifest=get',
                          response_dict={}),
                mock.call('test_c_segments',
                          'test_sub_slo',
                          query_string='multipart-manifest=get')])

    def test_download_object_job_skip_identical_diff_dlo(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 30)
            f.write(b'b')
            f.flush()
            on_disk_md5 = md5(b'a' * 30 + b'b').hexdigest()
            segment_md5 = md5(b'a' * 10).hexdigest()

            mock_conn = mock.Mock()
            mock_conn.get_object.side_effect = [
                ({'x-object-manifest': 'test_c_segments/test_o/prefix'},
                 [b'']),
                ({'x-object-manifest': 'test_c_segments/test_o/prefix'},
                 [b'a' * 30])]
            mock_conn.get_container.side_effect = [
                (None, [{'name': 'test_o/prefix/1',
                         'bytes': 10, 'hash': segment_md5},
                        {'name': 'test_o/prefix/2',
                         'bytes': 10, 'hash': segment_md5}]),
                (None, [{'name': 'test_o/prefix/3',
                         'bytes': 10, 'hash': segment_md5}]),
                (None, [])]

            type(mock_conn).attempts = mock.PropertyMock(return_value=2)
            type(mock_conn).auth_end_time = mock.PropertyMock(return_value=14)
            expected_r = {
                'action': 'download_object',
                'container': 'test_c',
                'object': 'test_o',
                'success': True,
                'response_dict': {},
                'path': 'test_o',
                'pseudodir': False,
                'read_length': 30,
                'attempts': 2,
                'start_time': 0,
                'headers_receipt': 1,
                'finish_time': 2,
                'auth_end_time': mock_conn.auth_end_time,
            }

            options = self.opts.copy()
            options['out_file'] = f.name
            options['skip_identical'] = True
            s = SwiftService()
            with mock.patch('swiftclient.service.time', side_effect=range(3)):
                with mock.patch('swiftclient.service.get_conn',
                                return_value=mock_conn):
                    r = s._download_object_job(
                        conn=mock_conn,
                        container='test_c',
                        obj='test_o',
                        options=options)

            self._assertDictEqual(r, expected_r)

            self.assertEqual(mock_conn.get_container.mock_calls, [
                mock.call('test_c_segments',
                          delimiter=None,
                          prefix='test_o/prefix',
                          marker=''),
                mock.call('test_c_segments',
                          delimiter=None,
                          prefix='test_o/prefix',
                          marker='test_o/prefix/2'),
                mock.call('test_c_segments',
                          delimiter=None,
                          prefix='test_o/prefix',
                          marker='test_o/prefix/3')])
            self.assertEqual(mock_conn.get_object.mock_calls, [
                mock.call('test_c',
                          'test_o',
                          resp_chunk_size=65536,
                          headers={'If-None-Match': on_disk_md5},
                          query_string='multipart-manifest=get',
                          response_dict={}),
                mock.call('test_c',
                          'test_o',
                          resp_chunk_size=65536,
                          headers={'If-None-Match': on_disk_md5},
                          response_dict={})])

    def test_download_object_job_skip_identical_diff_nested_slo(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'a' * 29)
            f.flush()
            on_disk_md5 = md5(b'a' * 29).hexdigest()

            seg_etag = md5(b'a' * 10).hexdigest()
            submanifest = "[%s]" % ",".join(
                ['{"bytes":10,"hash":"%s"}' % seg_etag] * 2)
            submanifest_etag = md5(seg_etag.encode('ascii') * 2).hexdigest()
            manifest = "[%s]" % ",".join([
                '{"sub_slo":true,"name":"/test_c_segments/test_sub_slo",'
                '"bytes":20,"hash":"%s"}' % submanifest_etag,
                '{"bytes":10,"hash":"%s"}' % seg_etag])

            mock_conn = mock.Mock()
            mock_conn.get_object.side_effect = [
                ({'x-static-large-object': True,
                  'content-length': 30,
                  'etag': md5(submanifest_etag.encode('ascii') +
                              seg_etag.encode('ascii')).hexdigest()},
                 [manifest.encode('ascii')]),
                ({'x-static-large-object': True,
                  'content-length': 20,
                  'etag': submanifest_etag},
                 submanifest.encode('ascii')),
                ({'x-static-large-object': True,
                  'content-length': 30,
                  'etag': md5(submanifest_etag.encode('ascii') +
                              seg_etag.encode('ascii')).hexdigest()},
                 [b'a' * 30])]

            type(mock_conn).attempts = mock.PropertyMock(return_value=2)
            type(mock_conn).auth_end_time = mock.PropertyMock(return_value=14)
            expected_r = {
                'action': 'download_object',
                'container': 'test_c',
                'object': 'test_o',
                'success': True,
                'response_dict': {},
                'path': 'test_o',
                'pseudodir': False,
                'read_length': 30,
                'attempts': 2,
                'start_time': 0,
                'headers_receipt': 1,
                'finish_time': 2,
                'auth_end_time': mock_conn.auth_end_time,
            }

            options = self.opts.copy()
            options['out_file'] = f.name
            options['skip_identical'] = True
            s = SwiftService()
            with mock.patch('swiftclient.service.time', side_effect=range(3)):
                with mock.patch('swiftclient.service.get_conn',
                                return_value=mock_conn):
                    r = s._download_object_job(
                        conn=mock_conn,
                        container='test_c',
                        obj='test_o',
                        options=options)

            self._assertDictEqual(r, expected_r)
            self.assertEqual(mock_conn.get_object.mock_calls, [
                mock.call('test_c',
                          'test_o',
                          resp_chunk_size=65536,
                          headers={'If-None-Match': on_disk_md5},
                          query_string='multipart-manifest=get',
                          response_dict={}),
                mock.call('test_c_segments',
                          'test_sub_slo',
                          query_string='multipart-manifest=get'),
                mock.call('test_c',
                          'test_o',
                          resp_chunk_size=65536,
                          headers={'If-None-Match': on_disk_md5},
                          response_dict={})])
