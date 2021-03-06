from setuptools import setup


setup(
    name='brightsky',
    version='0.9.1',
    author='Jakob de Maeyer',
    author_email='jakob@naboa.de',
    packages=['brightsky'],
    install_requires=[
        'click',
        'coloredlogs',
        'falcon',
        'falcon-cors',
        'gunicorn',
        'huey[redis]',
        'parsel',
        'psycopg2-binary',
        'python-dateutil',
        'requests',
        'sentry-sdk',
    ],
)
