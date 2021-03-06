import logging
import os
import tempfile
import urllib.request as request
import warnings
from datetime import date, timedelta
from functools import cached_property
from typing import Union

import numpy as np
import pandas as pd
from netCDF4 import Dataset
from scipy.spatial import KDTree

from ..data.util import DownloadProgressBar

logger = logging.getLogger(__name__)


class SeaSurfaceTempDownloader:
    """
    NOAA High-resolution Blended Analysis of Daily SST. Data is
    from Sep 1981 and is on a 1/4 deg global grid.
    """

    def __init__(self,
                 locations: pd.DataFrame,
                 start_date: str,
                 end_date: str,
                 lat_col: str = 'LAT',
                 lon_col: str = 'LON'):
        """
        Download the sea surface temperature (SST) data from NOAA.
        If the land coordinates are given, np.nan values will be returned in the dataframe
        """
        self.sst_data_parent_dir = 'ftp://ftp.cdc.noaa.gov/Datasets/noaa.oisst.v2.highres/'
        self.temp_dir = tempfile.mkdtemp()

        try:
            self.locations = locations[[lat_col, lon_col]]
        except KeyError:
            logger.critical(f'{lat_col} and {lon_col} are both needed')
            raise

        self.start_date = start_date
        self.end_date = end_date

    @classmethod
    def from_tuples(cls,
                    locations: Union[tuple, list],
                    start_date: str,
                    end_date: str):
        """
        alternative constructor allows users to initialize the class from a list/tuple
        of coordinates.
        """

        location_peek = locations[0]
        if isinstance(location_peek, tuple) or isinstance(location_peek, list):
            locations_df = pd.DataFrame(locations)
            logger.critical('we assume the first arg is latitude and second arg is longitude')
        else:
            if isinstance(location_peek, float) or isinstance(location_peek, int):
                locations_df = pd.DataFrame([locations])
            else:
                raise TypeError('only two types of input are supported: 1. [10, 20] or [[10, 20], [30, 30]]')

        locations_df.columns = ['LAT', 'LON']
        return cls(locations=locations_df,
                   start_date=start_date,
                   end_date=end_date,
                   lat_col='LAT',
                   lon_col='LON')

    @staticmethod
    def read_dataset(local_data_dir):
        """
        convert the netCDF dataset into a dictionary

        we also use variable['lat'][:] to convert values to numpy type
        because doing this speed the slicing up significantly
        """
        sst_data = Dataset(local_data_dir, mode='r')
        lat = sst_data.variables['lat'][:]  # this makes it an np array; faster than original type
        lon = sst_data.variables['lon'][:]
        sst = sst_data.variables['sst'][:]
        time = sst_data.variables['time'][:]
        lon = np.array([term if term <= 180 else (term - 360) for term in lon])
        return {'lat': lat,
                'lon': lon,
                'time': time,
                'sst': sst}

    def get_lat_lon_index(self, lat, lon):
        """
        A helper function to get the lat and lon index of the sst data,
        which will be later used for slicing.

        Background info:
        The sst data could be sliced as sst[correct_date, correct_lat, correct_lon]
        We will find out the *index* of the closet locations provided by the
        user.
        """

        def build_2d_meshgrid(lat, lon):
            xx, yy = np.meshgrid(lat, lon)
            xx = xx.reshape(-1)
            yy = yy.reshape(-1)
            return np.array([xx, yy]).T

        lat_idx = np.array(range(len(lat)))
        lon_idx = np.array(range(len(lon)))

        lat_lon_matrix = build_2d_meshgrid(lat, lon)
        lat_lon_idx_matrix = build_2d_meshgrid(lat_idx, lon_idx)

        tree = KDTree(lat_lon_matrix)
        dist, idx = tree.query(self.locations)

        locations_in_original_data = lat_lon_idx_matrix[idx]
        lat_idx = locations_in_original_data[:, 0]
        lon_idx = locations_in_original_data[:, 1]
        return lat_idx, lon_idx

    def get_time_index(self, dataset, list_of_regular_time):
        """
        convert the user provide time such as 2010-01-01 to 2010-10-01 into dates in seconds.
        """
        timeline = dataset['time']
        regular_time_to_index = {str(date(1800, 1, 1) + timedelta(int(t))): i for i, t in enumerate(timeline)}

        time_index_list = []  # used for slicing later in the sst data
        for regular_time in list_of_regular_time:
            regular_time = str(regular_time).split(' ')[0]  # 1985-01-01 00:00:00
            time_index_list.append(regular_time_to_index[regular_time])
        return time_index_list

    @cached_property
    def remote_to_local_dir_mapping(self):
        """
        since all observations in one year are stored in the same file, we only have to determine how many years
        total to be downloaded
        based off this we get the {remote_url: (local_dir, dates_in_that_year)} mapping
        """
        from collections import defaultdict
        year_to_timestamp = defaultdict(list)
        for time_stamp in pd.date_range(self.start_date, self.end_date):
            year_to_timestamp[time_stamp.year].append(time_stamp)

        remote_to_local = defaultdict(list)
        for year in year_to_timestamp:
            local_dir = os.path.join(self.temp_dir, f'sst.day.mean.{year}.nc')
            remote_url = self.sst_data_parent_dir + f'sst.day.mean.{year}.nc'
            remote_to_local[remote_url].append([local_dir, year_to_timestamp[year]])
        return remote_to_local

    @staticmethod
    def download_to_local(link_in, dir_out):
        """
        download the sst information to local dir.
        :param link_in: the url pointing to the file to be downloaded
        :param dir_out: the local dir where the file is stored
        """
        if not os.path.exists(dir_out):
            with DownloadProgressBar(unit='B', unit_scale=True, miniters=1, desc=link_in.split('/')[-1]) as t:
                request.urlretrieve(link_in, dir_out, t.update_to)

    def download(self):
        """
        Note:
        this is how we want to reformat the file
        day1 lat1 lon1 sst1
        day1 lat2 lon2 sst2
        day1 lat3 lon3 sst3
        day2 lat1 lon1 sst4
        day2 lat2 lon2 sst5
        day2 lat3 lon3 sst6

        that's why use np.repeat for dates
        """
        result = []
        for year_count, (url, bundle) in enumerate(self.remote_to_local_dir_mapping.items()):
            local_dir, all_dates_in_that_year = bundle[0]
            logger.critical('-' * 10 + 'start downloading' + f' year #{year_count + 1}' + '-' * 10)
            self.download_to_local(url, local_dir)
            logger.critical('-' * 10 + 'finished downloading' + f' year #{year_count + 1}' + '-' * 10)
            dataset = self.read_dataset(local_dir)

            time_index = self.get_time_index(dataset=dataset, list_of_regular_time=all_dates_in_that_year)
            lat_index, lon_index = self.get_lat_lon_index(lat=dataset['lat'], lon=dataset['lon'])

            time_index_stretched_to_match_dim = np.repeat(time_index, len(lat_index))
            lat_index_stretched_to_match_dim = np.tile(lat_index, len(time_index))
            lon_index_stretched_to_match_dim = np.tile(lon_index, len(time_index))

            sst = dataset['sst'][time_index_stretched_to_match_dim,
                                 lat_index_stretched_to_match_dim,
                                 lon_index_stretched_to_match_dim]

            # get real value (not index) to prepare for the output dataframe
            lat = dataset['lat'][lat_index_stretched_to_match_dim]
            lon = dataset['lon'][lon_index_stretched_to_match_dim]

            data = np.array([np.repeat(all_dates_in_that_year, len(lat_index)), lat, lon, sst]).T
            df = pd.DataFrame(data, columns=['DATE', 'LAT', 'LON', 'SST'])
            result.append(df)

        # check if all is none, will raise a warning if so
        output = pd.concat(result).reset_index(drop=True)

        null_values = output['SST'].isnull().sum()
        if null_values == len(output):
            warnings.warn('all the sst measurements are null values'
                          'this is usually because the locations you provided are on the land')
        # missing data are marked as -9.96921e+36, we convert them to np.nan
        output.loc[output['SST'] < -9e35, 'SST'] = np.nan
        return output


def download_sst(locations, start_date, end_date):
    if isinstance(locations, pd.DataFrame):
        downloader = SeaSurfaceTempDownloader(locations=locations,
                                              start_date=start_date,
                                              end_date=end_date)
    elif isinstance(locations, (tuple, list)):
        downloader = SeaSurfaceTempDownloader.from_tuples(locations=locations,
                                                          start_date=start_date,
                                                          end_date=end_date)
    elif isinstance(locations, str) and len(locations) == 2:
        raise TypeError('we do not support state abbreviation when downloading sst,'
                        'since such data are only available on the sea.')
    elif isinstance(locations, str) and len(locations) > 2:
        raise TypeError('we do not support address as input when downloading sst,'
                        'since such data are only available on the sea')
    else:
        raise TypeError('only list of coordinates or datframes are supported')

    return downloader.download()

