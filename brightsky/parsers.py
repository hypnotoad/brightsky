import csv
import datetime
import io
import logging
import os
import re
import zipfile
from contextlib import suppress

import dateutil.parser
import yaml
from dateutil.tz import tzutc
from parsel import Selector

from brightsky.db import fetch
from brightsky.settings import settings
from brightsky.units import (
    celsius_to_kelvin, hpa_to_pa, kmh_to_ms, minutes_to_seconds)
from brightsky.utils import cache_path, download


class Parser:

    DEFAULT_URL = None

    @property
    def logger(self):
        if not hasattr(self, '_logger'):
            self._logger = logging.getLogger(self.__class__.__name__)
        return self._logger

    def __init__(self, path=None, url=None):
        self.url = url or self.DEFAULT_URL
        self.path = path
        if not self.path and self.url:
            self.path = cache_path(self.url)
        self.downloaded_files = set()

    def download(self):
        self.logger.info('Downloading "%s" to "%s"', self.url, self.path)
        download_path = download(self.url, self.path)
        if download_path:
            self.downloaded_files.add(download_path)

    def should_skip(self):
        return False

    def parse(self):
        raise NotImplementedError

    def cleanup(self):
        if not settings.KEEP_DOWNLOADS:
            for path in self.downloaded_files:
                self.logger.debug("Removing '%s'", path)
                with suppress(FileNotFoundError):
                    os.remove(path)


class MOSMIXParser(Parser):

    DEFAULT_URL = (
        'https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_S/'
        'all_stations/kml/MOSMIX_S_LATEST_240.kmz')

    ELEMENTS = {
        'TTT': 'temperature',
        'DD': 'wind_direction',
        'FF': 'wind_speed',
        'RR1c': 'precipitation',
        'SunD1': 'sunshine',
        'PPPP': 'pressure_msl',
    }

    def parse(self):
        self.logger.info("Parsing %s", self.path)
        sel = self.get_selector()
        timestamps = self.parse_timestamps(sel)
        source = self.parse_source(sel)
        self.logger.debug(
            'Got %d timestamps for source %s', len(timestamps), source)
        station_selectors = sel.css('Placemark')
        for i, station_sel in enumerate(station_selectors):
            records = self.parse_station(station_sel, timestamps, source)
            yield from self.sanitize_records(records)

    def get_selector(self):
        with zipfile.ZipFile(self.path) as zf:
            infolist = zf.infolist()
            assert len(infolist) == 1, f'Unexpected zip content in {self.path}'
            with zf.open(infolist[0]) as f:
                sel = Selector(f.read().decode('latin1'), type='xml')
        sel.remove_namespaces()
        return sel

    def parse_timestamps(self, sel):
        return [
            dateutil.parser.parse(ts)
            for ts in sel.css('ForecastTimeSteps > TimeStep::text').extract()]

    def parse_source(self, sel):
        return ':'.join(sel.css('ProductID::text, IssueTime::text').extract())

    def parse_station(self, station_sel, timestamps, source):
        station_id = station_sel.css('name::text').extract_first()
        station_name = station_sel.css('description::text').extract_first()
        lon, lat, height = station_sel.css(
            'coordinates::text').extract_first().split(',')
        records = {'timestamp': timestamps}
        for element, column in self.ELEMENTS.items():
            values_str = station_sel.css(
                f'Forecast[elementName="{element}"] value::text'
            ).extract_first()
            records[column] = [
                None if row[0] == '-' else float(row[0])
                for row in csv.reader(
                    re.sub(r'\s+', '\n', values_str.strip()).splitlines())
            ]
            assert len(records[column]) == len(timestamps)
        base_record = {
            'observation_type': 'forecast',
            'source': source,
            'station_id': station_id,
            'station_name': station_name,
            'lat': float(lat),
            'lon': float(lon),
            'height': float(height),
        }
        # Turn dict of lists into list of dicts
        return (
            {**base_record, **dict(zip(records, row))}
            for row in zip(*records.values())
        )

    def sanitize_records(self, records):
        for r in records:
            if r['precipitation'] and r['precipitation'] < 0:
                self.logger.warning(
                    "Ignoring negative precipitation value: %s", r)
                r['precipitation'] = None
            if r['wind_direction'] and r['wind_direction'] > 360:
                self.logger.warning(
                    "Fixing out-of-bounds wind direction: %s", r)
                r['wind_direction'] -= 360
            yield r


class CurrentObservationsParser(Parser):

    ELEMENTS = {
        'dry_bulb_temperature_at_2_meter_above_ground': 'temperature',
        'mean_wind_direction_during_last_10 min_at_10_meters_above_ground': (
            'wind_direction'),
        'mean_wind_speed_during last_10_min_at_10_meters_above_ground': (
            'wind_speed'),
        'precipitation_amount_last_hour': 'precipitation',
        'pressure_reduced_to_mean_sea_level': 'pressure_msl',
        'total_time_of_sunshine_during_last_hour': 'sunshine',
    }
    DATE_COLUMN = 'surface observations'
    HOUR_COLUMN = 'Parameter description'

    CONVERTERS = {
        'pressure_msl': hpa_to_pa,
        'sunshine': minutes_to_seconds,
        'temperature': celsius_to_kelvin,
        'wind_speed': kmh_to_ms,
    }

    def parse(self, lat=None, lon=None, height=None, station_name=None):
        with open(self.path) as f:
            reader = csv.DictReader(f, delimiter=';')
            station_id = next(reader)[self.DATE_COLUMN].rstrip('_')
            if any(x is None for x in (lat, lon, height, station_name)):
                lat, lon, height, station_name = self.load_location(station_id)
            # Skip row with German header titles
            next(reader)
            for row in reader:
                yield {
                    'observation_type': 'current',
                    'station_id': station_id,
                    'station_name': station_name,
                    'lat': lat,
                    'lon': lon,
                    'height': height,
                    **self.parse_row(row)
                }

    def parse_row(self, row):
        record = {
            element: (
                None
                if row[column] == '---'
                else float(row[column].replace(',', '.')))
            for column, element in self.ELEMENTS.items()
        }
        record['timestamp'] = datetime.datetime.strptime(
            f'{row[self.DATE_COLUMN]} {row[self.HOUR_COLUMN]}',
            '%d.%m.%y %H:%M'
        ).replace(tzinfo=tzutc())
        self.convert_units(record)
        return record

    def convert_units(self, record):
        for element, converter in self.CONVERTERS.items():
            if record[element] is not None:
                record[element] = converter(record[element])

    def load_location(self, station_id):
        rows = fetch(
            """
            SELECT lat, lon, height, station_name
            FROM sources
            WHERE observation_type = %s AND station_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            ('forecast', station_id),
        )
        if not rows:
            raise ValueError(
                f'Unable to find location for station {station_id}')
        return rows[0]


class ObservationsParser(Parser):

    elements = {}
    converters = {}

    @property
    def _ignored_values(self):
        if not hasattr(ObservationsParser, '_ignored_values'):
            try:
                with open(settings.IGNORED_VALUES_PATH) as f:
                    ignored = yaml.safe_load(f)
            except FileNotFoundError:
                ignored = {}
            for file_ignored in ignored.values():
                for timestamp_str in list(file_ignored):
                    timestamp = datetime.datetime.fromisoformat(
                        timestamp_str).replace(tzinfo=tzutc())
                    file_ignored[timestamp] = file_ignored.pop(timestamp_str)
            ObservationsParser._ignored_values = ignored
        return ObservationsParser._ignored_values.get(self.url, {})

    def should_skip(self):
        if (m := re.search(r'_(\d{8})_(\d{8})_hist\.zip$', str(self.path))):
            end_date = datetime.datetime.strptime(
                m.group(2), '%Y%m%d').replace(tzinfo=tzutc())
            if end_date < settings.MIN_DATE:
                return True
            if settings.MAX_DATE:
                start_date = datetime.datetime.strptime(
                    m.group(1), '%Y%m%d').replace(tzinfo=tzutc())
                if start_date > settings.MAX_DATE:
                    return True
        return False

    def parse(self):
        with zipfile.ZipFile(self.path) as zf:
            station_id = self.parse_station_id(zf)
            observation_type = self.parse_observation_type()
            lat_lon_history = self.parse_lat_lon_history(zf, station_id)
            for record in self.parse_records(zf, lat_lon_history):
                self.sanitize_record(record)
                yield {
                    'observation_type': observation_type,
                    'station_id': station_id,
                    **record
                }

    def parse_station_id(self, zf):
        for filename in zf.namelist():
            if (m := re.match(r'Metadaten_Geographie_(\d+)\.txt', filename)):
                return m.group(1)
        raise ValueError(f"Unable to parse station ID for {self.path}")

    def parse_observation_type(self):
        filename = os.path.basename(self.path)
        if filename.endswith('_akt.zip'):
            return 'recent'
        elif filename.endswith('_hist.zip'):
            return 'historical'
        raise ValueError(
            f'Unable to determine observation type from path "{self.path}"')

    def parse_lat_lon_history(self, zf, station_id):
        with zf.open(f'Metadaten_Geographie_{station_id}.txt') as f:
            reader = csv.DictReader(
                io.TextIOWrapper(f, encoding='latin1'),
                delimiter=';')
            history = {}
            for row in reader:
                date_from = datetime.datetime.strptime(
                    row['von_datum'].strip(), '%Y%m%d'
                ).replace(tzinfo=tzutc())
                history[date_from] = (
                    float(row['Geogr.Breite']),
                    float(row['Geogr.Laenge']),
                    float(row['Stationshoehe']),
                    row['Stationsname'])
            return history

    def parse_records(self, zf, lat_lon_history):
        product_filenames = [
            fn for fn in zf.namelist() if fn.startswith('produkt_')]
        assert len(product_filenames) == 1, "Unexpected product count"
        filename = product_filenames[0]
        with zf.open(filename) as f:
            reader = csv.DictReader(
                io.TextIOWrapper(f, encoding='latin1'),
                delimiter=';')
            for row in reader:
                timestamp = datetime.datetime.strptime(
                    row['MESS_DATUM'], '%Y%m%d%H').replace(tzinfo=tzutc())
                if timestamp < settings.MIN_DATE:
                    continue
                elif settings.MAX_DATE and timestamp > settings.MAX_DATE:
                    continue
                for date, lat_lon_height_name in lat_lon_history.items():
                    if date > timestamp:
                        break
                    lat, lon, height, station_name = lat_lon_height_name
                yield {
                    'source': f'Observations:Recent:{filename}',
                    'lat': lat,
                    'lon': lon,
                    'height': height,
                    'station_name': station_name,
                    'timestamp': timestamp,
                    **self.parse_elements(row),
                }

    def parse_elements(self, row):
        elements = {
            element: (
                float(row[element_key])
                if row[element_key].strip() != '-999'
                else None)
            for element, element_key in self.elements.items()
        }
        for element, converter in self.converters.items():
            if elements[element] is not None:
                elements[element] = converter(elements[element])
        return elements

    def sanitize_record(self, record):
        ignores = self.ignored_values.get(record['timestamp'], {})
        for element, ignored_value in ignores.items():
            if record[element] == ignored_value:
                record[element] = None
            else:
                self.logger.warning(
                    "Ignored value '%s' of element '%s' for %s at %s no "
                    "longer exists",
                    ignored_value, element, self.url, record['timestamp'])


class TemperatureObservationsParser(ObservationsParser):

    elements = {
        'temperature': 'TT_TU',
    }
    converters = {
        'temperature': celsius_to_kelvin,
    }


class PrecipitationObservationsParser(ObservationsParser):

    elements = {
        'precipitation': '  R1',
    }


class WindObservationsParser(ObservationsParser):

    elements = {
        'wind_speed': '   F',
        'wind_direction': '   D',
    }


class SunshineObservationsParser(ObservationsParser):

    elements = {
        'sunshine': 'SD_SO',
    }
    converters = {
        'sunshine': minutes_to_seconds,
    }


class PressureObservationsParser(ObservationsParser):

    elements = {
        'pressure_msl': '  P0',
    }
    converters = {
        'pressure_msl': hpa_to_pa,
    }


def get_parser(filename):
    parsers = {
        r'MOSMIX_S_LATEST_240\.kmz$': MOSMIXParser,
        r'\w{5}-BEOB\.csv$': CurrentObservationsParser,
        'stundenwerte_FF_': WindObservationsParser,
        'stundenwerte_P0_': PressureObservationsParser,
        'stundenwerte_RR_': PrecipitationObservationsParser,
        'stundenwerte_SD_': SunshineObservationsParser,
        'stundenwerte_TU_': TemperatureObservationsParser,
    }
    for pattern, parser in parsers.items():
        if re.match(pattern, filename):
            return parser
