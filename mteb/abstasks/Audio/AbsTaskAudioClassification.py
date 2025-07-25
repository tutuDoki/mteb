from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import numpy as np
from sklearn.model_selection import KFold

from mteb.abstasks.TaskMetadata import HFSubset

from ...encoder_interface import AudioEncoder
from ...evaluation.evaluators import AudiologRegClassificationEvaluator
from ..AbsTask import AbsTask, ScoresDict

logger = logging.getLogger(__name__)


class AbsTaskAudioClassification(AbsTask):
    """Abstract class for audio classification tasks
    The similarity is computed between pairs and the results are ranked.

    self.load_data() must generate a huggingface dataset with a split matching self.metadata_dict["eval_splits"], and assign it to self.dataset. It
    must contain the following columns:
        audio: datasets.Audio
        label: int
    """

    audio_column_name: str = "audio"
    label_column_name: str = "labels"
    is_cross_validation: bool = False
    n_splits = 5  # by default: 5-fold cross-validation

    def __init__(
        self,
        method: str = "logReg",
        n_experiments: int | None = None,
        samples_per_label: int | None = None,
        k: int = 3,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.method = method

        # Bootstrap parameters
        self.n_experiments: int = (  # type: ignore
            n_experiments
            if n_experiments is not None
            else self.metadata_dict.get("n_experiments", 5)
        )
        self.samples_per_label: int = (  # type: ignore
            samples_per_label
            if samples_per_label is not None
            else self.metadata_dict.get("samples_per_label", 16)
        )

        # kNN parameters
        self.k = k

    def _add_main_score(self, scores: dict[HFSubset, ScoresDict]) -> None:
        scores["main_score"] = scores[self.metadata.main_score]

    def _calculate_metrics_from_split(
        self, split: str, hf_subset: str | None = None, compute_overall: bool = False
    ):
        pass

    def evaluate(
        self,
        model: AudioEncoder,
        eval_split: str = "test",
        train_split: str = "train",
        *,
        encode_kwargs: dict[str, Any] = {},
        **kwargs,
    ) -> dict[HFSubset, ScoresDict]:
        if not self.data_loaded:
            self.load_data()

        scores = {}
        hf_subsets = list(self.dataset) if self.is_multilingual else ["default"]

        for hf_subset in hf_subsets:
            logger.info(
                f"\nTask: {self.metadata.name}, split: {eval_split}, subset: {hf_subset}. Running..."
            )

            if hf_subset not in self.dataset and hf_subset == "default":
                ds = self.dataset
            else:
                ds = self.dataset[hf_subset]

            if self.is_cross_validation:
                scores[hf_subset] = self._evaluate_subset_cross_validation(
                    model,
                    ds,
                    eval_split,
                    train_split,
                    encode_kwargs=encode_kwargs,
                    **kwargs,
                )
            else:
                scores[hf_subset] = self._evaluate_subset(
                    model,
                    ds,
                    eval_split,
                    train_split,
                    encode_kwargs=encode_kwargs,
                    **kwargs,
                )
            self._add_main_score(scores[hf_subset])

        return scores

    def _evaluate_subset_cross_validation(
        self,
        model: AudioEncoder,
        dataset,
        eval_split: str = "test",
        train_split: str = "train",
        encode_kwargs: dict[str, Any] = {},
        **kwargs,
    ) -> ScoresDict:
        assert train_split == eval_split, (
            f"Performing {self.n_splits}-fold cross validation, but the dataset has a train (`{train_split}`) and test split (`{eval_split}`)! Set `is_cross_validation` to False, and retry."
        )
        logger.info(
            f"Performing {self.n_splits}-fold cross-validation on the entire dataset!"
        )

        ds = dataset[train_split]
        num_samples = len(ds)
        kf = KFold(n_splits=self.n_splits, shuffle=True, random_state=42)

        params = {"k": self.k}
        params.update(kwargs)
        scores = []
        test_cache, idxs = (
            None,
            None,
        )  # we store idxs to make the shuffling reproducible

        for train_idx, val_idx in kf.split(
            range(num_samples)
        ):  # perform k-fold cross validation
            train_split = ds.select(train_idx)
            eval_split = ds.select(val_idx)

            # Bootstrap `self.samples_per_label` samples per label for each split
            undersampled_train, idxs = self._undersample_data(
                train_split,
                self.label_column_name,
                self.samples_per_label,
                idxs=idxs,
            )

            if self.method == "logReg":
                evaluator = AudiologRegClassificationEvaluator(
                    undersampled_train,
                    eval_split,
                    self.audio_column_name,
                    self.label_column_name,
                    task_name=self.metadata.name,
                    encode_kwargs=encode_kwargs,
                    **params,
                )
            else:
                raise ValueError(f"Method {self.method} not supported")

            scores_exp, test_cache = evaluator(model, test_cache=test_cache)
            scores.append(scores_exp)

        avg_scores: dict[str, Any] = {
            k: np.mean([s[k] for s in scores]) for k in scores[0].keys()
        }
        avg_scores["scores_per_experiment"] = scores
        return avg_scores

    def _evaluate_subset(
        self,
        model: AudioEncoder,
        dataset,
        eval_split: str = "test",
        train_split: str = "train",
        encode_kwargs: dict[str, Any] = {},
        **kwargs,
    ) -> ScoresDict:
        train_split = dataset[train_split]
        eval_split = dataset[eval_split]
        params = {"k": self.k}
        params.update(kwargs)

        scores = []
        test_cache, idxs = (
            None,
            None,
        )  # we store idxs to make the shuffling reproducible
        for i in range(self.n_experiments):
            logger.info(
                "=" * 10 + f" Experiment {i + 1}/{self.n_experiments} " + "=" * 10
            )
            # Bootstrap `self.samples_per_label` samples per label for each split
            undersampled_train, idxs = self._undersample_data(
                train_split,
                self.label_column_name,
                self.samples_per_label,
                idxs=idxs,
            )

            if self.method == "logReg":
                evaluator = AudiologRegClassificationEvaluator(
                    undersampled_train,
                    eval_split,
                    self.audio_column_name,
                    self.label_column_name,
                    task_name=self.metadata.name,
                    encode_kwargs=encode_kwargs,
                    **params,
                )
            else:
                raise ValueError(f"Method {self.method} not supported")

            scores_exp, test_cache = evaluator(model, test_cache=test_cache)
            scores.append(scores_exp)

        avg_scores: dict[str, Any] = {
            k: np.mean([s[k] for s in scores]) for k in scores[0].keys()
        }
        avg_scores["scores_per_experiment"] = scores
        return avg_scores

    def _undersample_data(
        self, dataset_split, label_column_name, samples_per_label, idxs=None
    ):
        """Undersample data to have samples_per_label samples of each label
        without loading all audio into memory.
        """
        if idxs is None:
            idxs = np.arange(len(dataset_split))
        np.random.shuffle(idxs)
        if not isinstance(idxs, list):
            idxs = idxs.tolist()
        label_counter = defaultdict(int)
        selected_indices = []

        labels = dataset_split[label_column_name]
        for i in idxs:
            label = labels[i]
            if label_counter[label] < samples_per_label:
                selected_indices.append(i)
                label_counter[label] += 1

        undersampled_dataset = dataset_split.select(selected_indices)
        return (
            undersampled_dataset,
            idxs,
        )
