from typing import List, Dict, Optional

import numpy as np
import pandas as pd

from lightwood.helpers.log import log
from lightwood.helpers.numeric import is_nan_numeric
from lightwood.mixer.base import BaseMixer
from lightwood.ensemble.base import BaseEnsemble
from lightwood.api.types import PredictionArguments
from lightwood.data.encoded_ds import EncodedDs
from lightwood.helpers.general import evaluate_accuracy


class BestOf(BaseEnsemble):
    """
    This ensemble acts as a mixer selector. 
    After evaluating accuracy for all internal mixers with the validation data, it sets the best mixer as the underlying model.
    """  # noqa
    indexes_by_accuracy: List[float]

    def __init__(self, target, mixers: List[BaseMixer], data: EncodedDs, dtype_dict, accuracy_functions,
                 args: PredictionArguments, ts_analysis: Optional[dict] = None) -> None:
        super().__init__(target, mixers, data, dtype_dict)

        score_list = []
        for _, mixer in enumerate(mixers):
            score_dict = evaluate_accuracy(
                data.data_frame,
                mixer(data, args)['prediction'],
                target,
                accuracy_functions,
                ts_analysis=ts_analysis
            )

            avg_score = np.mean(list(score_dict.values()))
            log.info(f'Mixer: {type(mixer).__name__} got accuracy: {avg_score}')

            if is_nan_numeric(avg_score):
                avg_score = -pow(2, 63)
                log.warning(f'Change the accuracy of mixer {type(mixer).__name__} to valid value: {avg_score}')

            score_list.append(avg_score)

        self.store_context(data.data_frame, ts_analysis)
        self.indexes_by_accuracy = list(reversed(np.array(score_list).argsort()))
        self.supports_proba = self.mixers[self.indexes_by_accuracy[0]].supports_proba
        log.info(f'Picked best mixer: {type(self.mixers[self.indexes_by_accuracy[0]]).__name__}')

    def __call__(self, ds: EncodedDs, args: PredictionArguments) -> pd.DataFrame:
        if args.all_mixers:
            predictions = {}
            for mixer in self.mixers:
                predictions[f'__mdb_mixer_{type(mixer).__name__}'] = mixer(ds, args=args)['prediction']
            return pd.DataFrame(predictions)
        else:
            for mixer_index in self.indexes_by_accuracy:
                mixer = self.mixers[mixer_index]
                try:
                    return mixer(ds, args=args)
                except Exception as e:
                    if mixer.stable:
                        raise(e)
                    else:
                        log.warning(f'Unstable mixer {type(mixer).__name__} failed with exception: {e}.\
                        Trying next best')

    def store_context(self, data: pd.DataFrame, ts_analysis: Dict[str, str]):
        if self.dtype_dict[self.target] == 'tsarray':
            context = pd.DataFrame()
            groups = [g for g in ts_analysis['group_combinations'] if g != '__default']
            for group in groups:
                col_map = {col: group for col, group in zip(ts_analysis['tss'].group_by, group)}
                for col, group in col_map.items():
                    filtered = data
                    filtered = filtered[filtered[col] == group]
                    context = context.append(filtered.iloc[-1])
            self.context = context
        else:
            self.context = pd.DataFrame()

    def get_context(self) -> pd.DataFrame:
        if self.dtype_dict[self.target] == 'tsarray':
            self.context['__mdb_make_predictions'] = False  # trigger infer mode
            self.context['__lw_preprocessed'] = True  # mark as preprocessed
            return self.context
        else:
            return self.context
