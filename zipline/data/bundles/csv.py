#
# Ingest stock csv files to create a zipline data bundle

import os

import numpy  as np
import pandas as pd
import datetime
from cn_stock_holidays.zipline.default_calendar import shsz_calendar
import requests
import sqlite3

from zipline.utils.cli import maybe_show_progress

def _cachpath(symbol, type_):
    return '-'.join((symbol.replace(os.path.sep, '_'), type_))

def _calc_minute_index(market_opens, minutes_per_day):
    minutes = np.zeros(len(market_opens) * minutes_per_day,
                       dtype='datetime64[ns]')
    deltas = np.arange(0, minutes_per_day, dtype='timedelta64[m]')
    for i, market_open in enumerate(market_opens):
        start = market_open.asm8
        minute_values = start + deltas
        start_ix = minutes_per_day * i
        end_ix = start_ix + minutes_per_day
        minutes[start_ix:end_ix] = minute_values
    return pd.to_datetime(minutes, utc=True, box=True)

def csv_bundle(symbols, csv_path_map, start=None, end=None):
    # Define our custom ingest function
    """
    Parameters
    ----------
    csv_path_map: dict[str -> str]
    asset_db_path
    minute_bar_path
    daily_bar_path
    adjustment_path
    """
    def ingest(environ,
               asset_db_writer,
               minute_bar_writer,
               daily_bar_writer,
               adjustment_writer,
               calendar,
               start_session,
               end_session,
               cache,
               show_progress,
               output_dir,
               # pass these as defaults to make them 'nonlocal' in py2
               start=start,
               end=end):

        if start is None:
            start = start_session
        if end is None:
            end = None

        metadata = pd.DataFrame(np.empty(len(symbols), dtype=[
            ('start_date', 'datetime64[ns]'),
            ('end_date', 'datetime64[ns]'),
            ('auto_close_date', 'datetime64[ns]'),
            ('symbol', 'object'),
        ]))

        #My implementation
        csv_asset_data = None
        if 'asset_db_path' in csv_path_map.keys():
            csv_asset_data = pd.read_csv(csv_path_map['asset_db_path'], index_col='date', parse_dates=['date'])
        csv_minute_data = None
        if 'minute_bar_path' in csv_path_map.keys():
            csv_minute_data = pd.read_csv(csv_path_map['minute_bar_path'], index_col='date', parse_dates=['date'])
        csv_adjustment_data = None
        if 'adjustment_path' in csv_path_map.keys():
            csv_adjustment_data = pd.read_csv(csv_path_map['adjustment_path'], dtype=[
            ('symbol', 'object'),
            ('ex_date', 'object'),
            ('record_date', 'object'),
            ('declare_date', 'object'),
            ('pay_date', 'object'),
            ('amount', 'float64'),
            ('ratio', 'float64'),
            ('split', 'float64'),
            ],parse_dates=['ex_date', 'record_date','declared_date','pay_date'])

        sid = 0
        sids = []
        dfs = []
        dfMinutes = []
        splits = []
        df_divs = []
        df_divStock = []
        for symbol in symbols:

            tmpCsvData = csv_asset_data[csv_asset_data['symbol'] == symbol].copy()
            tmpCsvData = tmpCsvData[[ c for c in tmpCsvData.columns if c != 'symbol']]
            df = tmpCsvData.sort_index()
                
            tmpMinuteDate = csv_minute_data[csv_minute_data['symbol'] == symbol].copy()
            tmpMinuteDate = tmpMinuteDate[[c for c in tmpMinuteDate.columns if c != 'symbol']]
            tmpMinuteDate = tmpMinuteDate.sort_index()

            # the start date is the date of the first trade and
            # the end date is the date of the last trade
            start_date = df.index[0]
            end_date = df.index[-1]
            # The auto_close date is the day after the last trade.
            ac_date = end_date + pd.Timedelta(days=1)

            metadata.iloc[sid] = start_date, end_date, ac_date, symbol
            new_index = ['open', 'high', 'low', 'close', 'volume']
            df = df.reindex(columns = new_index, copy=False) #fix bug
            tmpMinuteDate = tmpMinuteDate.reindex(columns = new_index, copy=False) #fix bug

            sessions = calendar.sessions_in_range(start_date, end_date)
            df = df.reindex(
                        sessions.tz_localize(None),
                        copy=False,
                    ).fillna(0.0)
            tmpMinuteDate.index = tmpMinuteDate.index.tz_localize(calendar.tz).tz_convert('utc')

            minTS = pd.DatetimeIndex([tmpMinuteDate.index.min().date()])[0]
            maxTS = pd.DatetimeIndex([tmpMinuteDate.index.max().date()])[0]
            tmpSchedule = calendar.schedule[(calendar.schedule.index >= minTS) & (calendar.schedule.index <= maxTS)]
            tmpTimeDelta = tmpSchedule.iloc[0]['market_close'] - tmpSchedule.iloc[0]['market_open']
            minutes_per_day = int(tmpTimeDelta.seconds / 60 + 1.)
            minIdx = _calc_minute_index(tmpSchedule['market_open'], minutes_per_day)
            tmpMinuteDate = tmpMinuteDate.reindex(
                            minIdx,
                            copy=False,
                        ).fillna(0.0)
            sids.append(sid)
            dfs.append(df)
            dfMinutes.append(tmpMinuteDate)

            #splits
            split_ratios = csv_adjustment_data[(csv_adjustment_data['symbol'] == symbol) & (csv_adjustment_data['split'] != 0.)]
            if len(split_ratios) > 0.:
                split_ratios = split_ratios[['pay_date','split']]
                split_ratios['ratio'] = 1./ split_ratios['split']
                df_split = split_ratios[['pay_date','ratio']]
                df_split.rename(columns={'pay_date':'effective_date'}, inplace=True)
                df_split.set_index('effective_date', inplace=True)
                df_split.reset_index(inplace=True)
                df_split['sid'] = sid
                splits.append(df_split)
            # dividends
            divsAmount = csv_adjustment_data[(csv_adjustment_data['symbol'] == symbol)&(csv_adjustment_data['amount'] != 0.)]
            if len(divsAmount) > 0.:
                divsAmount = divsAmount[['ex_date','declared_date','record_date','pay_date','amount']]
                divsAmount = divsAmount[divsAmount['amount'] != 0.]
                divsAmount['sid'] = sid
                df_divs.append(divsAmount)
            #stock dividends
            divStock = csv_adjustment_data[csv_adjustment_data['symbol'] == symbol]
            if len(divStock) > 0.:
                divStock = divStock[['ex_date', 'declared_date', 'record_date', 'pay_date', 'ratio']]
                divStock = divStock[divStock['ratio'] != 0.]
                divStock['sid'] = sid
                divStock['payment_sid'] = sid # assume the stock dividend is only itselt
                df_divStock.append(divStock)

            sid += 1
        pricing_iter = zip(sids, dfs)
        minutes_iter = zip(sids, dfMinutes)


        daily_bar_writer.write(pricing_iter, show_progress=False)
        minute_bar_writer.write(minutes_iter, show_progress=False)
        metadata['exchange'] = "CSV"
        asset_db_writer.write(equities=metadata)
        adjustment_writer.write(splits = pd.concat(splits, ignore_index=True) if len(splits) > 0 else None,
                                dividends = pd.concat(df_divs, ignore_index=True) if len(df_divs) > 0 else None,
                                stock_dividends = pd.concat(df_divStock, ignore_index=True) if len(df_divStock) > 0 else None)
        adjustment_writer.close()
    return ingest
