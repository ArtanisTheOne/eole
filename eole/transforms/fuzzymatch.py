from eole.utils.logging import logger
from eole.transforms import register_transform
from .transform import Transform, TransformConfig
from pydantic import Field
import numpy as np
import time


class FuzzyMatchConfig(TransformConfig):
    tm_path: str | None = Field(default=None, description="Path to a flat text TM.")
    fuzzy_corpus_ratio: float | None = Field(default=0.1, description="Ratio of corpus to augment with fuzzy matches.")
    fuzzy_threshold: float | None = Field(default=70, description="The fuzzy matching threshold.")
    tm_delimiter: str | None = Field(default="\t", description="The delimiter used in the flat text TM.")
    fuzzy_token: str | None = Field(default="｟fuzzy｠", description="The fuzzy token to be added with the matches.")
    fuzzymatch_min_length: int | None = Field(default=4, description="Min length for TM entries and examples to match.")
    fuzzymatch_min_length: int | None = Field(
        default=70, description="Max length for TM entries and examples to match."
    )


class FuzzyMatcher(object):
    """Class for creating and setting up fuzzy matchers."""

    def __init__(
        self,
        tm_path,
        corpus_ratio,
        threshold=70,
        tm_delimiter="\t",
        fuzzy_token="｟fuzzy｠",
        tm_unit_min_lentgh=4,
        tm_unit_max_length=70,
    ):
        self.threshold = threshold
        self.corpus_ratio = corpus_ratio
        self.tm_delimiter = tm_delimiter
        self.fuzzy_token = fuzzy_token
        self.tm_unit_min_length = tm_unit_min_lentgh
        self.tm_unit_max_length = tm_unit_max_length
        self.internal_tm = self._create_tm(tm_path)

    def _create_tm(self, tm_path):
        """The TM should be a utf-8 text file with each line
        containing a source sentence and its translation, separated
        by the `self.tm_delimiter`. A TM size of 200k-250k pairs should
        provide enough matches and good performance, but this may
        depend on overall system specs (RAM, CPU)
        """

        src_segments, tgt_segments = list(), list()
        with open(tm_path, mode="r", encoding="utf-8") as file:
            pairs = file.readlines()
            for pair in pairs:
                source, target = map(str, pair.split(self.tm_delimiter))

                # Filter out very short or very long sentences
                # from the TM for better performance
                if len(source) < self.tm_unit_min_length or len(source) > self.tm_unit_max_length:
                    continue
                src_segments.append(source.strip())
                tgt_segments.append(target.strip())
        logger.debug(f"Translation Memory size for FuzzyMatch transform: " f"{len(src_segments)}")
        return [src_segments, tgt_segments]

    def _get_batch_matches(self, batch):
        from rapidfuzz import fuzz, process

        logger.debug(f"Starting fuzzy matching on {len(batch)} examples")
        fuzzy_count = 0
        start = time.time()
        augmented = list()

        # We split the `batch` and perform fuzzy matching
        # in smaller chunks of 10.000 examples in order to
        # reduce memory usage.
        # Perfomance is not affected.
        chunk_size = 10000
        mini_batches = np.array_split(batch, len(batch) // chunk_size if len(batch) > chunk_size else 1)
        for mini_batch in mini_batches:
            plist = list(mini_batch)
            if fuzzy_count >= len(batch) * self.corpus_ratio:
                augmented.extend(plist)
                continue

            results = process.cdist(
                plist,
                self.internal_tm[0],
                scorer=fuzz.ratio,
                dtype=np.uint8,
                score_cutoff=self.threshold,
                workers=-1,
            )

            matches = np.any(results, 1)
            argmax = np.argmax(results, axis=1)
            for idx, s in enumerate(plist):
                # Probably redundant but let's be safe
                # in case some examples are already fuzzied
                # (e.g. from another pipeline or workflow)
                if self.fuzzy_token in s:
                    continue
                # We don't want exact matches
                if matches[idx] and results[idx][argmax[idx]] < 100:
                    if fuzzy_count >= len(batch) * self.corpus_ratio:
                        break
                    plist[idx] = s + self.fuzzy_token + self.internal_tm[1][argmax[idx]]
                    fuzzy_count += 1
            augmented.extend(plist)

        end = time.time()
        logger.debug(f"FuzzyMatch Transform: Added {fuzzy_count} " f"fuzzies in {end-start} secs")

        return augmented


@register_transform(name="fuzzymatch")
class FuzzyMatchTransform(Transform):
    """Perform fuzzy matching against a translation memory and
    augment source examples with target matches for Neural Fuzzy Repair.
    :cite:`bulte-tezcan-2019-neural`
    """

    def __init__(self, config):
        super().__init__(config)

    def _parse_config(self):
        self.tm_path = self.config.tm_path
        self.fuzzy_corpus_ratio = self.config.fuzzy_corpus_ratio
        self.fuzzy_threshold = self.config.fuzzy_threshold
        self.tm_delimiter = self.config.tm_delimiter
        self.fuzzy_token = self.config.fuzzy_token
        self.fuzzymatch_min_length = self.config.fuzzymatch_min_length
        self.fuzzymatch_max_length = self.config.fuzzymatch_max_length

    @classmethod
    def get_specials(cls, config):
        """Add the fuzzy match token to the src vocab."""

        return ([config.fuzzy_token], list())

    def warm_up(self, vocabs=None):
        """Create the fuzzy matcher."""

        super().warm_up(None)
        self.matcher = FuzzyMatcher(
            self.tm_path,
            self.fuzzy_corpus_ratio,
            self.fuzzy_threshold,
            self.tm_delimiter,
            self.fuzzy_token,
            self.fuzzymatch_min_length,
            self.fuzzymatch_max_length,
        )

    def apply(self, example, is_train=False, stats=None, **kwargs):
        return example

    def batch_apply(self, batch, is_train=False, stats=None, **kwargs):
        src_segments = list()
        for ex, _, _ in batch:
            # Apply a basic filtering to leave out very short or very long
            # sentences and speed up things a bit during fuzzy matching
            if (
                len(" ".join(ex["src"])) > self.fuzzymatch_min_length
                and len(" ".join(ex["src"])) < self.fuzzymatch_max_length
            ):
                src_segments.append(" ".join(ex["src"]))
            else:
                src_segments.append("")
        fuzzied_src = self.matcher._get_batch_matches(src_segments)
        assert len(src_segments) == len(fuzzied_src)
        for idx, (example, _, _) in enumerate(batch):
            if fuzzied_src[idx] != "":
                example["src"] = fuzzied_src[idx].split(" ")

        return batch
