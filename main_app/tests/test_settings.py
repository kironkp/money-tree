from django.conf import settings
from django.test import SimpleTestCase


class TestEnvironmentIsDeterministic(SimpleTestCase):
    def test_unit_tests_do_not_require_a_collected_static_manifest(self):
        backend = settings.STORAGES['staticfiles']['BACKEND']
        self.assertEqual(backend, 'django.contrib.staticfiles.storage.StaticFilesStorage')
