from string import uppercase, lowercase, digits
from random import SystemRandom
from os.path import abspath, split as path_split, expanduser, isdir
from os import makedirs
import sys
import shutil
import subprocess

from datacats.docker import (web_command, run_container, remove_container,
    inspect_container)
from datacats.validate import valid_name

def generate_db_password():
    """
    Return a 16-character alphanumeric random string generated by the
    operating system's secure pseudo random number generator
    """
    chars = uppercase + lowercase + digits
    return ''.join(SystemRandom().choice(chars) for x in xrange(16))

def write(s):
    sys.stdout.write(s)
    sys.stdout.flush()

def main(opts):
    workdir, name = path_split(abspath(opts['PROJECT']))

    if not valid_name(name):
        print 'Please choose a project name starting with a letter and'
        print 'includig only lowercase letters and digits'
        return

    if not isdir(workdir):
        print 'Parent directory for project does not exist!'
        return

    datadir = expanduser('~/.datacats/' + name)
    target = workdir + '/' + name

    if isdir(datadir):
        print 'Project data directory {0} already exists.'.format(datadir)
        return
    if isdir(target):
        print 'Project directory already exists.'
        return

    postgres_password = generate_db_password()
    ckan_password = generate_db_password()
    datastore_ro_password = generate_db_password()
    datastore_rw_password = generate_db_password()

    write('Creating project "{0}"'.format(name))

    makedirs(datadir, mode=0700)
    makedirs(datadir + '/venv')
    makedirs(datadir + '/search')
    makedirs(datadir + '/data')
    makedirs(datadir + '/files')
    makedirs(target + '/conf')
    makedirs(target + '/src')
    write('.')

    ckan_version='master'

    # copy virtualenv
    web_command(
        command='/bin/cp -a /usr/lib/ckan/. /usr/lib/ckan_target/.',
        rw={datadir + '/venv': '/usr/lib/ckan_target'},
        preload_ckan_version=ckan_version)
    write('.')

    # copy ckan source
    web_command(
        command='/bin/cp -a /project/src/. /project/src_target/.',
        rw={target + '/src': '/project/src_target'},
        preload_ckan_version=ckan_version)
    shutil.copy(
        target + '/src/ckan/ckan/config/who.ini',
        target + '/conf')
    shutil.copy(
        target + '/src/ckan/ckan/config/solr/schema.xml',
        target + '/conf')
    write('.')

    # set ownership of file storage dir to container apache user id
    web_command(
        command='/bin/chown -R www-data: /var/www/storage',
        rw={datadir + '/files': '/var/www/storage'})
    write('.')

    # create initial ckan.ini
    web_command(
        command='/usr/lib/ckan/bin/paster make-config'
            ' ckan /etc/ckan/default/ckan.ini',
        ro={datadir + '/venv': '/usr/lib/ckan',
            target + '/src': '/project/src'},
        rw={target + '/conf': '/etc/ckan/default'})
    write('.')

    # update ckan.ini with our host names and passwords
    # FIXME: using config-tool inside the container, would be better to
    # do this locally on the config file
    web_command(
        command=[
            '/usr/lib/ckan/bin/paster',
            '--plugin=ckan',
            'config-tool',
            '/etc/ckan/default/ckan.ini',
            '-e',
            'sqlalchemy.url = postgresql://ckan:{0}@db:5432/ckan'.format(
                ckan_password),
            'ckan.datastore.read_url = postgresql://ckan_datastore_readonly:'
                '{0}@db:5432/ckan_datastore'.format(datastore_ro_password),
            'ckan.datastore.write_url = postgresql://ckan_datastore_readwrite:'
                '{0}@db:5432/ckan_datastore'.format(datastore_rw_password),
            'solr_url = http://solr:8080/solr',
            'ckan.storage_path = /var/www/storage',
            ],
        ro={datadir + '/venv': '/usr/lib/ckan',
            target + '/src': '/project/src'},
        rw={target + '/conf': '/etc/ckan/default'})
    write('.')

    # set ownership of project files to local user id
    web_command(
        command='/bin/chown -R --reference=/etc/ckan/default'
            ' /usr/lib/ckan /project/src /etc/ckan/default',
        rw={datadir + '/venv': '/usr/lib/ckan',
            target + '/src': '/project/src',
            target + '/conf': '/etc/ckan/default'})
    write('.')

    # postgres container needs all its user passwords on first run
    run_container(
        name='datacats_data_' + name,
        image='datacats/data',
        environment={'POSTGRES_PASSWORD': postgres_password,
            'CKAN_PASSWORD': ckan_password,
            'DATASTORE_RO_PASSWORD': datastore_ro_password,
            'DATASTORE_RW_PASSWORD': datastore_rw_password},
        rw={datadir + '/data': '/var/lib/postgresql/data'})
    run_container(
        name='datacats_search_' + name,
        image='datacats/search',
        rw={datadir + '/search': '/var/lib/solr'},
        ro={target + '/conf/schema.xml': '/etc/solr/conf/schema.xml'})
    write('.')

    # ckan db init
    web_command(
        command='/usr/lib/ckan/bin/paster --plugin=ckan db init'
            ' -c /etc/ckan/default/ckan.ini',
        ro={datadir + '/venv': '/usr/lib/ckan',
            target + '/src': '/project/src',
            target + '/conf': '/etc/ckan/default'},
        links={'datacats_search_' + name: 'solr',
            'datacats_data_' + name: 'db'})
    write('.')

    if opts['--image-only']:
        remove_container('datacats_data_' + name)
        remove_container('datacats_search_' + name)
        write('\n')
    else:
        run_container(
            name='datacats_web_' + name,
            image='datacats/web',
            rw={datadir + '/files': '/var/www/storage'},
            ro={datadir + '/venv': '/usr/lib/ckan',
                target + '/src': '/project/src',
                target + '/conf': '/etc/ckan/default'},
            links={'datacats_search_' + name: 'solr',
                'datacats_data_' + name: 'db'})
        write('.\n')
        info = inspect_container('datacats_web_' + name)
        ip = info['NetworkSettings']['IPAddress']
        write('Site available at http://{0}/\n'.format(ip))

    if opts['--no-sysadmin']:
        return

    # FIXME: consider switching this to dockerpty
    # using subprocess for docker client's interactive session
    subprocess.call([
        '/usr/bin/docker', 'run', '--rm', '-it',
        '-v', datadir + '/venv:/usr/lib/ckan:ro',
        '-v', target + '/src:/project/src:ro',
        '-v', target + '/conf:/etc/ckan/default:ro',
        '--link', 'datacats_search_' + name + ':solr',
        '--link', 'datacats_data_' + name + ':db',
        'datacats/web', '/usr/lib/ckan/bin/paster', '--plugin=ckan',
        'sysadmin', 'add', 'admin', '-c' '/etc/ckan/default/ckan.ini'])
