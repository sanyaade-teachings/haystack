import logging
from pathlib import Path
from typing import List, Optional, Union

import numpy as np
import torch
from sentence_transformers import CrossEncoder

from haystack.modeling.utils import initialize_device_settings
from haystack.nodes.sampler.base import BaseSampler
from haystack.schema import Document

logger = logging.getLogger(__name__)


class TopPSampler(BaseSampler):
    """
    Filters documents based on the cumulative probability of the similarity scores between the
    query and the documents using top p sampling.

    Top p sampling selects a subset of the most relevant data points from a larger set of data. The technique
    involves calculating the cumulative probability of the scores of each data point, and then
    selecting the top p percent of data points with the highest cumulative probability.

    In the context of TopPSampler, the `run()` method takes in a query and a set of documents,
    calculates the similarity scores between the query and the documents, and then filters
    the documents based on the cumulative probability of these scores. The TopPSampler provides a
    way to efficiently select the most relevant documents based on their similarity to a given query.

    Usage example:

    ```python
    search = WebSearch(api_key="<your_api_key_here>")
    sampler = TopPSampler(top_p=0.95)

    p = Pipeline()
    p.add_node(component=search, name="Search", inputs=["Query"])
    p.add_node(component=sampler, name="Sampler", inputs=["Search"])
    print(p.run(query="What's the secret of the Universe?"))
    ```
    """

    def __init__(
        self,
        model_name_or_path: Union[str, Path] = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        model_version: Optional[str] = None,
        top_p: Optional[float] = 1.0,
        strict: Optional[bool] = False,
        top_score_field: Optional[str] = "score",
        use_gpu: Optional[bool] = True,
        devices: Optional[List[Union[str, torch.device]]] = None,
        use_auth_token: Optional[Union[str, bool]] = None,
    ):
        """
        Initialize a TopPSampler.

        :param model_name_or_path: Path to a pretrained sentence-transformers model.
        :param model_version: The version of the model to use. Can be a tag name, a branch name, or a commit hash.
        :param top_p: Cumulative probability threshold for filtering the documents (usually between 0.9 and 0.99).
        :param strict: If `top_p` is set to a low value and sampler returned no documents, then setting `strict` to
        `False` ensures at least one document is returned. If `strict` is set to `True`, then no documents are returned.
        :param top_score_field: The name of the score that should be used to insert the scores into the meta field
        of the document.
        :param use_gpu: Whether to use GPU (if available). If no GPUs are available, it falls back on a CPU.
        :param devices: List of torch devices (for example, cuda:0, cpu, mps) to limit inference to specific devices.
        :param use_auth_token: The token to use as the HTTP bearer authorization for remote files.

        """
        super().__init__()

        self.top_p = top_p
        self.top_score_field = top_score_field
        self.strict = strict
        self.devices, _ = initialize_device_settings(devices=devices, use_cuda=use_gpu, multi_gpu=True)
        self.cross_encoder = CrossEncoder(model_name_or_path, device=str(self.devices[0]))

    def predict(self, query: str, documents: List[Document], top_p: Optional[float] = None) -> List[Document]:
        """
        Returns a list of documents filtered using `top_p`, based on the similarity scores between the query and the
        documents whose cumulative probability is less than or equal to `top_p`.

        :param query: Query string.
        :param documents: List of Document.
        :param top_p: Cumulative probability threshold for filtering the documents. If not provided, the top_p value
        set during TopPSampler initialization is used.
        :return: List of Documents sorted by (desc.) similarity with the query.
        """
        if top_p is None:
            top_p = self.top_p if self.top_p else 1.0

        if not documents:
            return []

        # prepare the data for the cross encoder
        query_doc_pairs = [[query, doc.content] for doc in documents]

        # compute the similarity scores for these combinations
        similarity_scores = self.cross_encoder.predict(query_doc_pairs)

        # Apply softmax normalization to the similarity scores
        probs = np.exp(similarity_scores) / np.sum(np.exp(similarity_scores))

        # Sort the probabilities and calculate their cumulative sum
        sorted_probs = np.sort(probs)[::-1]
        cumulative_probs = np.cumsum(sorted_probs)

        # Find the indices with cumulative probabilities that exceed top_p
        top_p_indices = np.where(cumulative_probs <= top_p)[0]

        # Map the selected indices back to their original indices
        original_indices = np.argsort(probs)[::-1][top_p_indices]
        # and select the top_p responses
        selected_docs = [documents[i] for i in original_indices]

        # if strict is set to False, low p resulted in no documents being selected, then
        # return at least one document
        if not selected_docs and not self.strict:
            highest_prob_indices = np.argsort(probs)[::-1]
            selected_docs = [documents[highest_prob_indices[0]]]

        # include prob scores in the results
        if self.top_score_field:
            for idx, doc in enumerate(selected_docs):
                doc.meta[self.top_score_field] = str(sorted_probs[idx])
        return selected_docs

    def predict_batch(
        self,
        queries: List[str],
        documents: Union[List[Document], List[List[Document]]],
        top_p: Optional[float] = None,
        batch_size: Optional[int] = None,
    ) -> Union[List[Document], List[List[Document]]]:
        """
         - If you provide a list containing a single query...

            - ... and a single list of Documents, the single list of Documents is be re-ranked based on the
              supplied query.
            - ... and a list of lists of Documents, each list of Documents is re-ranked individually based on the
              supplied query.


        - If you provide a list of multiple queries, provide a list of lists of Documents. Each list of Documents
        is re-ranked based on its corresponding query.
        """
        if top_p is None:
            top_p = self.top_p

        # TODO: add support for batch_size if possible

        if len(queries) == 1 and isinstance(documents[0], Document):
            return self.predict(queries[0], documents, top_p)  # type: ignore

        if len(queries) == 1 and isinstance(documents[0], list):
            return [self.predict(queries[0], docs, top_p) for docs in documents]  # type: ignore

        if len(queries) > 1 and isinstance(documents[0], list):
            return [self.predict(query, docs, top_p) for query, docs in zip(queries, documents)]  # type: ignore

        raise ValueError(
            f"The input is not valid. Provided were the following queries {queries} and documents {documents}"
            f"Check the method documentation regarding the valid parameters and their types."
        )
