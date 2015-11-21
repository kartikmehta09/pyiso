from datetime import timedelta
import copy
from bs4 import BeautifulSoup
from pyiso.base import BaseClient
from pyiso import LOGGER
import pandas as pd
from io import StringIO
import re


class ERCOTClient(BaseClient):
    NAME = 'ERCOT'
    base_report_url = 'http://mis.ercot.com'

    report_type_ids = {
        'wind_5min': '13071',
        'wind_hrly': '13028',
        'gen_hrly': '12358',
        'load_7day': '12311',
    }

    TZ_NAME = 'US/Central'

    def utcify(self, local_ts, hour_ending=False, **kwargs):
        # ERCOT is hour ending, want hour beginning
        if hour_ending:
            hour_beginning_ts = local_ts - timedelta(hours=1)
        else:
            hour_beginning_ts = local_ts

        return super(ERCOTClient, self).utcify(hour_beginning_ts, **kwargs)

    def _request_report(self, report_type):
        # request reports list
        params = {'reportTypeId': self.report_type_ids[report_type]}
        report_list_contents = self.request(self.base_report_url+'/misapp/GetReports.do',
                                            params=params).content
        report_list_soup = BeautifulSoup(report_list_contents)

        # find the endpoint to download
        report_endpoint = None
        for elt in report_list_soup.find_all('tr'):
            label = elt.find(class_='labelOptional_ind')
            if label:
                if 'csv' in label.string:
                    report_endpoint = self.base_report_url + elt.a.attrs['href']
                    break

        # test endpoint found
        if not report_endpoint:
            raise ValueError('ERCOT: No report available for %s, soup:\n%s' % (report_type, report_list_soup))

        # read report from zip
        r = self.request(report_endpoint)
        if r:
            content = self.unzip(r.content)
        else:
            return pd.DataFrame()

        # parse csv
        df = pd.read_csv(StringIO(content[0].decode('unicode_escape')))
        df.columns = [x.strip() for x in df.columns]
        df = df.dropna(axis=0)

        # return
        return df

    def is_dst(self, val, standard):
        return val != standard

    def get_generation(self, latest=False, **kwargs):
        # get nonwind gen data
        raw_gen_df = self._request_report('gen_hrly')
        total_dp = raw_gen_df.iloc[0]
        total_gen = float(total_dp['SE_MW'])

        # get timestamp on hour
        # TODO is this what this timestamp means??
        raw_ts = self.utcify(total_dp['SE_EXE_TIME'],
                             hour_ending=True,
                             is_dst=self.is_dst(total_dp['SE_EXE_TIME_DST'], 's'))
        ts_hour_rounded_down = raw_ts.replace(minute=0, second=0, microsecond=0)
      #  if raw_ts.minute > 30:
      #      ts_hour_rounded = ts_hour_rounded_down + timedelta(hours=1)
      #  else:
      #      ts_hour_rounded = ts_hour_rounded_down

        # process wind data
        wind_gen = None
        wind_df = self._request_report('wind_hrly')
        for irow, wind_dp in wind_df.iterrows():
            wind_ts = self.utcify(wind_dp['HOUR_BEGINNING'],
                                  hour_ending=False,
                                  is_dst=self.is_dst(wind_dp['DSTFlag'], 'N'))
            if wind_ts == ts_hour_rounded_down:
                try:
                    wind_gen = float(wind_dp['ACTUAL_SYSTEM_WIDE'])
                except ValueError:  # empty string
                    wind_gen = None
                    LOGGER.error('No wind data available at %s in ERCOT' % (raw_ts))
                break

        # set up storage
        parsed_data = []
        base_dp = {'timestamp': ts_hour_rounded_down,
                   'freq': self.FREQUENCY_CHOICES.hourly, 'market': self.MARKET_CHOICES.hourly,
                   'gen_MW': 0, 'ba_name': self.NAME}

        # collect parsed data
        if wind_gen is not None:
            nonwind_gen = total_gen - wind_gen
            for gen_MW, fuel_name in [(wind_gen, 'wind'), (nonwind_gen, 'nonwind')]:
                parsed_dp = copy.deepcopy(base_dp)
                parsed_dp['fuel_name'] = fuel_name
                parsed_dp['gen_MW'] = gen_MW
                parsed_data.append(parsed_dp)

        # return
        return parsed_data

    def get_load(self, latest=False, **kwargs):
        # set args
        self.handle_options(data='load', latest=latest, **kwargs)

        if self.options['latest']:
            # get latest load site
            response = self.request('http://www.ercot.com/content/cdr/html/real_time_system_conditions.html')

            # parse load from response
            data = self.parse_rtm_load(response.text)

        elif self.options['forecast']:
            # get 7 day forecast load
            df = self._request_report('load_7day')

            # convert column of hour ending (1:00-24:00) to hour beginning (0:00-23:00)
            df['HourBeginning'] = df.apply(lambda dp: int(dp['HourEnding'].split(':')[0])-1,
                                           axis=1)

            # create datetime index of hour beginning
            df.index = df.apply(lambda dp: self.utcify(pd.to_datetime('%s %d:00' % (dp['DeliveryDate'], dp['HourBeginning'])),
                                                       hour_ending=False,
                                                       is_dst=self.is_dst(dp['DSTFlag'], 'N')),
                                axis=1)

            # slice times
            sliced = self.slice_times(df)

            # pull out total load series
            series = sliced['SystemTotal']
            series.name = 'load_MW'
            series.index.set_names(['timestamp'], inplace=True)

            # slice and format
            extras = {
                'ba_name': self.NAME,
                'market': self.MARKET_CHOICES.dam,
                'freq': self.FREQUENCY_CHOICES.hourly,
            }
            data = self.serialize_faster(series, extras=extras)

        else:
            raise ValueError('Load only available for latest or forecast in ERCOT')

        # return
        return data

    def parse_rtm_load(self, content):
        # make soup
        soup = BeautifulSoup(content)

        # load is after 'Actual System Demand' text
        load_label_elt = soup.find(text='Actual System Demand')
        load_parent_elt = load_label_elt.parent.parent.parent
        load_elt = load_parent_elt.find(class_='labelValueClassBold')
        load_val = float(load_elt.text)

        # timestamp text starts with 'Last Updated'
        timestamp_elt = soup.find(text=re.compile('Last Updated'))
        timestamp_str = timestamp_elt.strip('Last Updated ')
        timestamp = self.utcify(timestamp_str)

        # assemble dp
        dp = {
            'timestamp': timestamp,
            'ba_name': self.NAME,
            'market': self.options.get('market', self.MARKET_CHOICES.fivemin),
            'freq': self.options.get('freq', self.FREQUENCY_CHOICES.fivemin),
            'load_MW': load_val,
        }

        # return
        return [dp]
