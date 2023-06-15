import json
import os
from typing import Optional, Dict, Union, List, Any
import logging

import requests

from haystack.environment import HAYSTACK_REMOTE_API_TIMEOUT_SEC, HAYSTACK_REMOTE_API_MAX_RETRIES
from haystack.errors import CohereInferenceLimitError, CohereUnauthorizedError, CohereError
from haystack.utils import request_with_retry

from haystack.errors import HaystackError
from haystack.schema import Document
from haystack.nodes.ranker.base import BaseRanker


logger = logging.getLogger(__name__)


TIMEOUT = float(os.environ.get(HAYSTACK_REMOTE_API_TIMEOUT_SEC, 30))
RETRIES = int(os.environ.get(HAYSTACK_REMOTE_API_MAX_RETRIES, 5))


class CohereRanker(BaseRanker):
    """
    Re-Ranking can be used on top of a retriever to boost the performance for document search.
    This is particularly useful if the retriever has a high recall but is bad in sorting the documents by relevance.
    """

    def __init__(
        self,
        api_key: str,
        model_name_or_path: str,
        top_k: int = 10,
        return_documents: bool = False,
        max_chunks_per_doc: Optional[int] = None,
    ):
        """
         Creates an instance of CohereInvocationLayer for the specified Cohere model

        :param api_key: Cohere API key
        :param model_name_or_path: Cohere model name
        :param top_k: The maximum number of documents to return.
        :param return_documents: If false, returns results without the doc text - the api will return a list of
            {index, relevance score} where index is inferred from the list passed into the request.
            If true, returns results with the doc text passed in - the api will return an ordered list of
            {index, text, relevance score} where index + text refers to the list passed into the request.
        :param max_chunks_per_doc: If your document exceeds 512 tokens, this will determine the maximum number of
            chunks a document can be split into. For example, if your document is 6000 tokens, with the default of 10,
            the document will be split into 10 chunks each of 512 tokens and the last 880 tokens will be disregarded.
        """
        super().__init__()
        valid_api_key = isinstance(api_key, str) and api_key
        if not valid_api_key:
            raise ValueError(
                f"api_key {api_key} must be a valid Cohere token. "
                f"Your token is available in your Cohere settings page."
            )
        # See model info at https://docs.cohere.com/docs/models
        # supported models are rerank-english-v2.0, rerank-multilingual-v2.0
        valid_model_name_or_path = isinstance(model_name_or_path, str) and model_name_or_path
        if not valid_model_name_or_path:
            raise ValueError(f"model_name_or_path {model_name_or_path} must be a valid Cohere model name")
        self.model_name_or_path = model_name_or_path
        self.api_key = api_key
        self.top_k = top_k
        self.return_documents = return_documents
        self.max_chunks_per_doc = max_chunks_per_doc

    @property
    def url(self) -> str:
        return "https://api.cohere.ai/v1/rerank"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Request-Source": "python-sdk",
        }

    def _post(
        self,
        data: Dict[str, Any],
        stream: bool = False,
        attempts: int = RETRIES,
        status_codes_to_retry: Optional[List[int]] = None,
        timeout: float = TIMEOUT,
    ) -> requests.Response:
        """
        Post data to the Cohere inference model. It takes in a prompt and returns a list of responses using a REST
        invocation.
        :param data: The data to be sent to the model.
        :param stream: Whether to stream the response.
        :param attempts: The number of attempts to make.
        :param status_codes_to_retry: The status codes to retry on.
        :param timeout: The timeout for the request.
        :return: The response from the model as a requests.Response object.
        """
        response: requests.Response
        if status_codes_to_retry is None:
            status_codes_to_retry = [429]
        try:
            response = request_with_retry(
                method="POST",
                status_codes_to_retry=status_codes_to_retry,
                attempts=attempts,
                url=self.url,
                headers=self.headers,
                json=data,
                timeout=timeout,
                stream=stream,
            )
        except requests.HTTPError as err:
            res = err.response
            if res.status_code == 429:
                raise CohereInferenceLimitError(f"API rate limit exceeded: {res.text}")
            if res.status_code == 401:
                raise CohereUnauthorizedError(f"API key is invalid: {res.text}")

            raise CohereError(
                f"Cohere model returned an error.\nStatus code: {res.status_code}\nResponse body: {res.text}",
                status_code=res.status_code,
            )
        return response

    def predict(self, query: str, documents: List[Document], top_k: Optional[int] = None) -> List[Document]:
        """
        Use the Cohere Reranker to re-rank the supplied list of documents
        """
        if top_k is None:
            top_k = self.top_k

        # See https://docs.cohere.com/reference/rerank-1
        cohere_docs = [{"text": d.content} for d in documents]
        # TODO Instead of truncating, loop over in batches of 1000
        if len(cohere_docs) > 1000:
            logger.warning(
                "The Cohere reranking endpoint only supports 1000 documents. "
                "The the number of documents has been truncated to 1000."
            )
            cohere_docs = cohere_docs[:1000]

        params = {
            "model": self.model_name_or_path,
            "query": query,
            "documents": cohere_docs,
            "top_n": None,  # By passing None we return all documents and use top_k to truncate later
            "return_documents": self.return_documents,
            "max_chunks_per_doc": self.max_chunks_per_doc,
        }
        response = self._post(params)
        output = json.loads(response.text)

        indices = [o["index"] for o in output["results"]]
        scores = [o["relevance_score"] for o in output["results"]]
        sorted_docs = []
        for idx, score in zip(indices, scores):
            doc = documents[idx]
            doc.score = score
            sorted_docs.append(documents[idx])

        return sorted_docs[:top_k]

    def predict_batch(
        self,
        queries: List[str],
        documents: Union[List[Document], List[List[Document]]],
        top_k: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> Union[List[Document], List[List[Document]]]:
        """
        Use Cohere Reranking endpoint to re-rank the supplied lists of Documents.

        Returns a lists of Documents sorted by (desc.) similarity with the corresponding queries.

        - If you provide a list containing a single query...

            - ... and a single list of Documents, the single list of Documents will be re-ranked based on the
              supplied query.
            - ... and a list of lists of Documents, each list of Documents will be re-ranked individually based on the
              supplied query.

        - If you provide a list of multiple queries...

            - ... you need to provide a list of lists of Documents. Each list of Documents will be re-ranked based on
              its corresponding query.

        :param queries: Single query string or list of queries
        :param documents: Single list of Documents or list of lists of Documents to be reranked.
        :param top_k: The maximum number of documents to return per Document list.
        :param batch_size: Not relevant.
        """
        if top_k is None:
            top_k = self.top_k

        if len(documents) > 0 and isinstance(documents[0], Document):
            # Docs case 1: single list of Documents -> rerank single list of Documents based on single query
            if len(queries) != 1:
                raise HaystackError("Number of queries must be 1 if a single list of Documents is provided.")
            return self.predict(query=queries[0], documents=documents, top_k=top_k)  # type: ignore
        else:
            # Docs case 2: list of lists of Documents -> rerank each list of Documents based on corresponding query
            # If queries contains a single query, apply it to each list of Documents
            if len(queries) == 1:
                queries = queries * len(documents)
            if len(queries) != len(documents):
                raise HaystackError("Number of queries must be equal to number of provided Document lists.")

            results = []
            for query, cur_docs in zip(queries, documents):
                results.append(self.predict(query=query, documents=cur_docs, top_k=top_k))  # type: ignore
            return results
