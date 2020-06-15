"""
read.py

Read raw input files without modification of the underlying data, apart from type conversions
or formatting.
"""

# Import standard or builtin libraries
import os
import re
import numpy as np
import pandas as pd

# Import local modules and variables
from parameters import states_nhd, pwc_durations
from paths import condensed_soil_path, met_attributes_path, combo_path, crop_dates_path, \
    crop_params_path, gen_params_path, irrigation_path, \
    preprocessed_path, pwc_scenario_path, crop_group_path, gdd_input_path, reconstituted_path
from efed_lib.efed_lib import report
from parameters import fields


def test_path(f):
    def wrapped(*args, **kwargs):
        try:
            r = f(*args, **kwargs)
            return r
        except Exception as e:
            raise e

    return wrapped


def combinations(region, years, nrows=None):
    """
    Read a table of soil/land cover/weather/watershed combinations for generating
    scenario files.
    :param region: NHD Plus Hydroregion (str)
    :param years: Years to fetch data for (iter, int)
    :param nrows: Number of rows to read (int, optional)
    :return: Combinations table (df)
    """
    all_combos = None
    for year in years:
        header = ['gridcode', 'cdl', 'weather_grid', 'mukey', 'area']
        combo_file = combo_path.format(region, year)
        combos = pd.read_csv(combo_file, dtype=np.uint32, nrows=nrows)[header]
        combos['year'] = np.int16(year)
        all_combos = combos if all_combos is None else pd.concat([all_combos, combos], axis=0)
    return all_combos


def crop(region):
    """
    Read data from parameter tables linked to land use and combine into a single table
    for generating field scenarios.
    :param region: NHD+ Hydroregion (str)
    :return: Table of parameters linked to land use (df)
    """
    fields.refresh()

    # Read CDL/crop group index
    index_fields, dtypes = fields.fetch('CropGroups', dtypes=True)
    crop_index = pd.read_csv(crop_group_path, usecols=index_fields, dtype=dtypes)

    # Read parameters indexed to CDL
    param_fields, dtypes = fields.fetch('CropParams', dtypes=True, col='external_name')
    crop_params = pd.read_csv(crop_params_path, usecols=param_fields, dtype=dtypes)

    # Read crop dates
    date_fields, dtypes = fields.fetch('CropDates', dtypes=True)
    crop_dates = pd.read_csv(crop_dates_path, usecols=date_fields, dtype=dtypes)
    # Convert dates to days since Jan 1
    for field in fields.fetch('date'):
        crop_dates[field] = (pd.to_datetime(crop_dates[field], format=date_fmt) - pd.to_datetime("1900-01-01")).dt.days
    # Where harvest is before plant, add 365 days (e.g. winter wheat)
    for stage in ['begin', 'end', 'begin_active', 'end_active']:
        crop_dates.loc[crop_dates[f'plant_{stage}'] > crop_dates[f'harvest_{stage}'], f'harvest_{stage}'] += 365

    # Read irrigation parameters
    irrigation_fields, dtypes = fields.fetch('Irrigation', dtypes=True)
    irrigation_data = pd.read_csv(irrigation_path, usecols=irrigation_fields, dtype=dtypes)

    # Read parameters indexed to crop groups
    group_fields, dtypes = fields.fetch('CurveNumbers', dtypes=True)
    group_params = pd.read_csv(gen_params_path, usecols=group_fields, dtype=dtypes)
    group_params = group_params[group_params.region == region]

    data = crop_index.merge(crop_params, on=['cdl', 'cdl_alias'], how='left') \
        .merge(crop_dates, on=['cdl', 'cdl_alias'], how='left', suffixes=('', '_burn')) \
        .merge(irrigation_data, on=['cdl_alias', 'state'], how='left') \
        .merge(group_params, on='pwc_class', how='left', suffixes=('_cdl', '_gen'))

    data[['evergreen', 'alt_date']] = data[['evergreen', 'alt_date']].fillna(0).astype(bool)

    return data


def gdd(grapes=False):
    if not grapes:
        table = pd.read_csv(gdd_input_path)
        for col in table.columns.values:
            if col.endswith("temp_f"):
                table[col.rstrip('_f')] = (table.pop(col) - 32.) * (5 / 9)
    else:
        raise FutureWarning("Haven't implemented grapes yet")
    table = table.dropna(subset=['emergence_gdd', 'maxcover_gdd', 'emergence_base_temp', 'maxcover_base_temp'])
    return table


def met():
    """
    Read data tables indexed to weather grid
    :return: Table of parameters indexed to weather grid
    """
    field_names, dtypes = fields.fetch("MetParams", dtypes=True)
    met_data = pd.read_csv(met_attributes_path, usecols=field_names, dtype=dtypes)
    # met_data = met_data.rename(columns={"stationID": 'weather_grid'})  # these combos have old weather grids?
    return met_data


def soils(mode, region=None, state=None):
    """
    Read and aggregate all soils data for an NHD Hydroregion or state
    :param mode: 'sam' or 'pwc'
    :param region: NHD Hydroregion (str, optional)
    :param state: State abbreviation (str, optional)
    :return: Table of parameters indexed to soil map unit (df)
    """
    fields.refresh()

    if region is None and state is not None:
        region_states = [state]
    else:
        region_states = states_nhd[region]
    state_tables = []
    valu_table = ssurgo("", "valu")
    for state in region_states:
        state_table = None
        for table_name, key_field in [('muaggatt', 'mukey'), ('component', 'mukey'), ('chorizon', 'cokey')]:
            table = ssurgo(state, table_name)
            state_table = table if state_table is None else pd.merge(state_table, table, on=key_field, how='outer')
        state_table['state'] = state
        state_tables.append(state_table)
    soil_data = pd.concat(state_tables, axis=0)
    soil_data = soil_data.merge(valu_table, on='mukey')

    return soil_data.rename(columns=fields.convert)


def ssurgo(state, name):
    """
    Read a condensed SSURGO soils data table
    :param state: State (str)
    :param name: Table name (str)
    :return:
    """
    table_fields, data_types = fields.fetch(name, dtypes=True, col='external_name')
    table_path = condensed_soil_path.format(state, name)
    return pd.read_csv(table_path, dtype=data_types, usecols=table_fields)


@test_path
def pwc_infile(class_num=None, class_name=None, region=None, use_parent=False, preprocessed=False, path=None):
    """
    Read a scenarios table as generated by scenarios_and_recipes.py. Add a line_num field that matches up
    with the PWC outfile
    :param region: NHD+ hydroregion (str)
    :param class_num: Land use identifier, usually CDL class (str)
    :param class_name: Name of CDL class (str)
    :param use_parent: Workaround (bool)
    :return: Dataframe of scenarios (df)
    """
    if path is None:
        if use_parent:
            path = pwc_scenario_path.format(region, 'parent')
        elif preprocessed:
            path = preprocessed_path.format(class_num, class_name)
        else:
            path = pwc_scenario_path.format(f'r{region}', class_num, class_name)
    table = pd.read_csv(path, dtype={'area': np.int64})
    if region is not None:
        table['region'] = region
    else:
        table['region'] = [str(region).zfill(2) for region in table.region]
    if use_parent:
        table = table[table.cdl == int(class_num)]
    return table


@test_path
def pwc_outfile(region=None, class_num=None, class_name=None, koc=None, preprocessed=False, in_file=None):
    """
    Read a PWC output file
    :param in_file: Path to PWC output file (str)
    :return: Dataframe of PWC output (df)
    """
    pwc_header = fields.fetch('pwc_id_fields') + pwc_durations
    if in_file is None:
        root = reconstituted_path if preprocessed else pwc_scenario_path
        in_file = root.format(region, class_num, class_name, koc)
    # Read the table, manually entering in the header (original header is tough to parse)
    if not preprocessed:
        table = pd.read_csv(in_file, names=pwc_header, delimiter=r'\s+')

        # Adjust line number so that header is not included
        table['line_num'] = table.line_num.astype(np.int32) - 1

        # Split the Batch Run ID field into constituent parts
        data = table.pop('run_id').str.split('_', expand=True)
        print(data.head())
        print(['bunk'] + fields.fetch('pwc_run_id'))
        data.columns = ['bunk'] + fields.fetch('pwc_run_id')
        table = pd.concat([data, table], axis=1)
        table = table.melt(id_vars=[f for f in table.columns if not f in pwc_durations], value_vars=pwc_durations,
                           var_name='duration', value_name='conc')
    else:
        table = pd.read_csv(in_file)
    return table