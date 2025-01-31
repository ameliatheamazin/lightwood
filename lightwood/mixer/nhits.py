from typing import Dict, Union

import numpy as np
import pandas as pd
from hyperopt import hp
import neuralforecast as nf
from neuralforecast.models.mqnhits.mqnhits import MQNHITS

from lightwood.helpers.log import log
from lightwood.mixer.base import BaseMixer
from lightwood.api.types import PredictionArguments
from lightwood.data.encoded_ds import EncodedDs, ConcatedEncodedDs


class NHitsMixer(BaseMixer):
    horizon: int
    target: str
    supports_proba: bool
    model_path: str
    hyperparam_search: bool
    default_config: dict

    def __init__(
            self,
            stop_after: float,
            target: str,
            horizon: int,
            ts_analysis: Dict,
            pretrained: bool = False
    ):
        """
        Wrapper around a MQN-HITS deep learning model.
        
        :param stop_after: time budget in seconds.
        :param target: column to forecast.
        :param horizon: length of forecasted horizon.
        :param ts_analysis: dictionary with miscellaneous time series info, as generated by 'lightwood.data.timeseries_analyzer'.
        """  # noqa
        super().__init__(stop_after)
        self.stable = True
        self.prepared = False
        self.supports_proba = False
        self.target = target
        self.horizon = horizon
        self.ts_analysis = ts_analysis
        self.grouped_by = ['__default'] if not ts_analysis['tss'].group_by else ts_analysis['tss'].group_by

        self.pretrained = pretrained  # finetuning?
        self.base_url = 'https://nixtla-public.s3.amazonaws.com/transfer/pretrained_models/'
        self.freq_to_model = {
            'Y': 'yearly',
            'Q': 'monthly',
            'M': 'monthly',
            'W': 'daily',
            'D': 'daily',
            'H': 'hourly',
            'T': 'hourly',  # consider using another pre-trained model once available
            'S': 'hourly'  # consider using another pre-trained model once available
        }
        self.model_names = {
            'hourly': 'nhits_m4_hourly.ckpt',  # hourly (non-tiny)
            'daily': 'nhits_m4_daily.ckpt',   # daily
            'monthly': 'nhits_m4_monthly.ckpt',  # monthly
            'yearly': 'nhits_m4_yearly.ckpt',  # yearly
        }
        self.model_name = None
        self.model = None

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """
        Fits the N-HITS model.
        """  # noqa
        log.info('Started fitting N-HITS forecasting model')

        cat_ds = ConcatedEncodedDs([train_data, dev_data])
        oby_col = self.ts_analysis["tss"].order_by
        df = cat_ds.data_frame.sort_values(by=f'__mdb_original_{oby_col}')

        # 2. adapt data into the expected DFs
        Y_df = self._make_initial_df(df)

        # set val-test cutoff
        n_time = len(df[f'__mdb_original_{oby_col}'].unique())
        n_ts_val = int(.1 * n_time)
        n_ts_test = int(.1 * n_time)

        # train the model
        n_time_out = self.horizon
        if self.pretrained:
            self.model_name = self.model_names.get(self.freq_to_model[self.ts_analysis['sample_freqs']['__default']],
                                                   None)
            self.model_name = self.model_names['hourly'] if self.model_name is None else self.model_name
            ckpt_url = self.base_url + self.model_name
            self.model = MQNHITS.load_from_checkpoint(ckpt_url)
            if self.horizon > self.model.hparams.n_time_out:
                self.pretrained = False

        if not self.pretrained:
            self.model = nf.auto.MQNHITS(horizon=n_time_out)
            self.model.space['max_steps'] = hp.choice('max_steps', [1e4])
            self.model.space['max_epochs'] = hp.choice('max_epochs', [50])
            self.model.space['n_time_in'] = hp.choice('n_time_in', [self.ts_analysis['tss'].window])
            self.model.space['n_time_out'] = hp.choice('n_time_out', [self.horizon])
            self.model.space['n_x_hidden'] = hp.choice('n_x_hidden', [0])
            self.model.space['n_s_hidden'] = hp.choice('n_s_hidden', [0])
            self.model.space['frequency'] = hp.choice('frequency', [self.ts_analysis['sample_freqs']['__default']])
            self.model.space['random_seed'] = hp.choice('random_seed', [42])
            self.model.fit(Y_df=Y_df,
                           X_df=None,       # Exogenous variables
                           S_df=None,       # Static variables
                           hyperopt_steps=5,
                           n_ts_val=n_ts_val,
                           n_ts_test=n_ts_test,
                           results_dir='./results/autonhits',
                           save_trials=False,
                           loss_function_val=nf.losses.numpy.mqloss,
                           loss_functions_test={'MQ': nf.losses.numpy.mqloss},
                           return_test_forecast=False,
                           verbose=True)

    def partial_fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """
        Due to how lightwood implements the `update` procedure, expected inputs for this method are:
        
        :param dev_data: original `test` split (used to validate and select model if ensemble is `BestOf`).
        :param train_data: concatenated original `train` and `dev` splits.
        """  # noqa
        self.hyperparam_search = False
        self.fit(dev_data, train_data)
        self.prepared = True

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        """
        Calls the mixer to emit forecasts.
        
        NOTE: in the future we may support predicting every single row efficiently. For now, this mixer
        replicates the neuralforecast library behavior and returns a forecast strictly for the next `tss.horizon`
        timesteps after the end of the input dataframe.
        """  # noqa
        if args.predict_proba:
            log.warning('This mixer does not output probability estimates')

        length = sum(ds.encoded_ds_lenghts) if isinstance(ds, ConcatedEncodedDs) else len(ds)
        ydf = pd.DataFrame(0,  # zero-filled
                           index=np.arange(length),
                           columns=['prediction', 'lower', 'upper'],
                           dtype=object)

        input_df = self._make_initial_df(ds.data_frame).reset_index()
        ydf['index'] = input_df['index']

        pred_cols = ['y_5', 'y_50', 'y_95']
        target_cols = ['lower', 'prediction', 'upper']
        for target_col in target_cols:
            ydf[target_col] = [[0 for _ in range(self.horizon)] for _ in range(len(ydf))]  # zero-filled arrays

        group_ends = []
        for group in input_df['unique_id'].unique():
            group_ends.append(input_df[input_df['unique_id'] == group]['index'].iloc[-1])
        fcst = self.model.forecast(Y_df=input_df)

        for gidx, group in zip(group_ends, input_df['unique_id'].unique()):
            for pred_col, target_col in zip(pred_cols, target_cols):
                group_preds = fcst[fcst['unique_id'] == group][pred_col].tolist()[:self.horizon]
                idx = ydf[ydf['index'] == gidx].index[0]
                ydf.at[idx, target_col] = group_preds

        ydf['confidence'] = 0.9
        return ydf

    def _make_initial_df(self, df):
        oby_col = self.ts_analysis["tss"].order_by
        Y_df = pd.DataFrame()
        Y_df['y'] = df[self.target]
        Y_df['ds'] = pd.to_datetime(df[f'__mdb_original_{oby_col}'], unit='s')
        if self.grouped_by != ['__default']:
            Y_df['unique_id'] = df[self.grouped_by].apply(lambda x: ','.join([elt for elt in x]), axis=1)
        else:
            Y_df['unique_id'] = ''
        return Y_df
