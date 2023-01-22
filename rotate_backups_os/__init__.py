#backups: Simple command line interface for backup rotation.
#
# Author: Peter Odding <peter@peterodding.com>
# Last Change: March 21, 2016
# URL: https://github.com/xolox/python-rotate-backups

"""
Simple to use Python API for rotation of backups.

The :mod:`rotate_backups` module contains the Python API of the
`rotate-backups` package. The core logic of the package is contained in the
:class:`RotateBackups` class.
"""

# Standard library modules.
import datetime
import fnmatch
import logging

# External dependencies.
from dateutil.relativedelta import relativedelta
from humanfriendly.text import concatenate

import boto3
from rotate_backups import Backup, RotateBackups, TIMESTAMP_PATTERN

# Semi-standard module versioning.
__version__ = '0.1'

# Initialize a logger for this module.
logger = logging.getLogger(__name__)

GLOBAL_CONFIG_FILE = '/etc/rotate-backups-os.ini'
"""The pathname of the system wide configuration file (a string)."""

LOCAL_CONFIG_FILE = '~/.rotate-backups-os.ini'
"""The pathname of the user specific configuration file (a string)."""

ORDERED_FREQUENCIES = (('hourly', relativedelta(hours=1)),
                       ('daily', relativedelta(days=1)),
                       ('weekly', relativedelta(weeks=1)),
                       ('monthly', relativedelta(months=1)),
                       ('yearly', relativedelta(years=1)))
"""
A list of tuples with two values each:

- The name of a rotation frequency (a string like 'hourly', 'daily', etc.).
- A :class:`~dateutil.relativedelta.relativedelta` object.

The tuples are sorted by increasing delta (intentionally).
"""

SUPPORTED_FREQUENCIES = dict(ORDERED_FREQUENCIES)
"""
A dictionary with rotation frequency names (strings) as keys and
:class:`~dateutil.relativedelta.relativedelta` objects as values. This
dictionary is generated based on the tuples in :data:`ORDERED_FREQUENCIES`.
"""


class S3RotateBackups(RotateBackups):

    """Python API for the ``rotate-backups-os`` program."""

    def __init__(self, rotation_scheme, access_key_id, secret_access_key, region_name=None, endpoint_url=None, 
                 include_list=None, exclude_list=None, dry_run=False,
                 config_file=None):
        """
        Construct a :class:`S3RotateBackups` object.

        :param rotation_scheme: A dictionary with one or more of the keys 'hourly',
                                'daily', 'weekly', 'monthly', 'yearly'. Each key is
                                expected to have one of the following values:

                                - An integer gives the number of backups in the
                                  corresponding category to preserve, starting from
                                  the most recent backup and counting back in
                                  time.
                                - The string 'always' means all backups in the
                                  corresponding category are preserved (useful for
                                  the biggest time unit in the rotation scheme).

                                By default no backups are preserved for categories
                                (keys) not present in the dictionary.
        :param include_list: A list of strings with :mod:`fnmatch` patterns. If a
                             nonempty include list is specified each backup must
                             match a pattern in the include list, otherwise it
                             will be ignored.
        :param exclude_list: A list of strings with :mod:`fnmatch` patterns. If a
                             backup matches the exclude list it will be ignored,
                             *even if it also matched the include list* (it's the
                             only logical way to combine both lists).
        :param dry_run: If this is ``True`` then no changes will be made, which
                        provides a 'preview' of the effect of the rotation scheme
                        (the default is ``False``). Right now this is only useful
                        in the command line interface because there's no return
                        value.
        :param io_scheduling_class: Use ``ionice`` to set the I/O scheduling class
                                    (expected to be one of the strings 'idle',
                                    'best-effort' or 'realtime').
        :param config_file: The pathname of a configuration file (a string).
        """
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.conn = boto3.client('s3', region_name = region_name,
            endpoint_url = endpoint_url,
            aws_access_key_id = access_key_id,
            aws_secret_access_key = secret_access_key)

        super(S3RotateBackups, self).__init__(rotation_scheme,
            include_list=include_list, exclude_list=exclude_list,
            dry_run=dry_run, config_file=config_file)

    def rotate_backups(self, bucketname):
        """
        Rotate the backups in a bucket according to a flexible rotation scheme.

        :param bucketname: Object Storage bucket that contains backups to rotate (a string).
        """

        # Collect the backups in the given directory.
        sorted_backups = self.collect_backups(bucketname)
        if not sorted_backups:
            logger.info("No backups found in %s.", bucketname)
            return
        most_recent_backup = sorted_backups[-1]
        # Group the backups by the rotation frequencies.
        backups_by_frequency = self.group_backups(sorted_backups)
        # Apply the user defined rotation scheme.
        self.apply_rotation_scheme(backups_by_frequency, most_recent_backup.timestamp)
        # Find which backups to preserve and why.
        backups_to_preserve = self.find_preservation_criteria(backups_by_frequency)
        # Apply the calculated rotation scheme.
        deleted_files = []
        for backup in sorted_backups:
            if backup in backups_to_preserve:
                matching_periods = backups_to_preserve[backup]
                logger.info("Preserving %s (matches %s retention %s) ..",
                    backup.pathname, concatenate(map(repr, matching_periods)),
                    "period" if len(matching_periods) == 1 else "periods"
                )
            else:
                logger.info("Deleting %s %s ..", backup.type, backup.pathname)
                if not self.dry_run:
                    logger.debug("Marking %s for deletion.", backup.pathname)
                    deleted_files.append(backup.pathname)
        if deleted_files:
            for file in deleted_files:
                self.conn.delete_object(Bucket=bucketname, Key=file)
                    
        if len(backups_to_preserve) == len(sorted_backups):
            logger.info("Nothing to do! (all backups preserved)")

    def collect_backups(self, bucket):
        """
        Collect the backups in the given Object Storage bucket.

        :param bucket: Object Storage backup bucket (a string).
        :returns: A sorted :class:`list` of :class:`Backup` objects (the
                  backups are sorted by their date).
        """
        backups = []
        
        logger.info("Scanning bucket for backups: %s", bucket)
        
        for entry in sorted([key for key in self.conn.list_objects(Bucket=bucket)["Contents"]], key=lambda d: d['LastModified']):
            # Check for a time stamp in the directory entry's name.
            match = TIMESTAMP_PATTERN.search(entry['LastModified'].strftime('%Y-%m-%d'))
            if match:
                # Make sure the entry matches the given include/exclude patterns.
                if self.exclude_list and any(fnmatch.fnmatch(entry['Key'], p) for p in self.exclude_list):
                    logger.debug("Excluded %r (it matched the exclude list).", entry)
                elif self.include_list and not any(fnmatch.fnmatch(entry['Key'], p) for p in self.include_list):
                    logger.debug("Excluded %r (it didn't match the include list).", entry)
                else:
                    backups.append(S3Backup(
                        pathname=entry['Key'],
                        timestamp=datetime.datetime(*(int(group, 10) for group in match.groups('0'))),
                    ))
            else:
                logger.debug("Failed to match time stamp in filename: %s", entry)
        if backups:
            logger.info("Found %i timestamped backups in %s.", len(backups), bucket)
        return sorted(backups)


class S3Backup(Backup):

    @property
    def type(self):
        """Get a string describing the type of backup (e.g. file, directory)."""
        return 's3_file'
