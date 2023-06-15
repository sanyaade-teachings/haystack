import copy
import re
from typing import Literal, Any, Dict, List, Optional, Iterable

import logging

import numpy as np
import pandas as pd
import rank_bm25
from tqdm.auto import tqdm

from haystack.preview.dataclasses import Document
from haystack.preview.document_stores.memory._filters import match
from haystack.preview.document_stores.errors import DuplicateDocumentError, MissingDocumentError
from haystack.utils.scipy_utils import expit

logger = logging.getLogger(__name__)
DuplicatePolicy = Literal["skip", "overwrite", "fail"]


class MemoryDocumentStore:
    """
    Stores data in-memory. It's ephemeral and cannot be saved to disk.
    """

    def __init__(
        self,
        bm25_tokenization_regex: str = r"(?u)\b\w\w+\b",
        bm25_algorithm: Literal["BM25Okapi", "BM25L", "BM25Plus"] = "BM25Okapi",
        bm25_parameters: Optional[Dict] = None,
    ):
        """
        Initializes the store.
        """
        self.storage: Dict[str, Document] = {}
        self.bm25_tokenization_regex = bm25_tokenization_regex
        self.bm25_algorithm = bm25_algorithm
        self.bm25_parameters = bm25_parameters or {}

    def count_documents(self) -> int:
        """
        Returns the number of how many documents are present in the document store.
        """
        return len(self.storage.keys())

    def filter_documents(self, filters: Optional[Dict[str, Any]] = None) -> List[Document]:
        """
        Returns the documents that match the filters provided.

        Filters are defined as nested dictionaries. The keys of the dictionaries can be a logical operator (`"$and"`,
        `"$or"`, `"$not"`), a comparison operator (`"$eq"`, `$ne`, `"$in"`, `$nin`, `"$gt"`, `"$gte"`, `"$lt"`,
        `"$lte"`) or a metadata field name.

        Logical operator keys take a dictionary of metadata field names and/or logical operators as value. Metadata
        field names take a dictionary of comparison operators as value. Comparison operator keys take a single value or
        (in case of `"$in"`) a list of values as value. If no logical operator is provided, `"$and"` is used as default
        operation. If no comparison operator is provided, `"$eq"` (or `"$in"` if the comparison value is a list) is used
        as default operation.

        Example:

        ```python
        filters = {
            "$and": {
                "type": {"$eq": "article"},
                "date": {"$gte": "2015-01-01", "$lt": "2021-01-01"},
                "rating": {"$gte": 3},
                "$or": {
                    "genre": {"$in": ["economy", "politics"]},
                    "publisher": {"$eq": "nytimes"}
                }
            }
        }
        # or simpler using default operators
        filters = {
            "type": "article",
            "date": {"$gte": "2015-01-01", "$lt": "2021-01-01"},
            "rating": {"$gte": 3},
            "$or": {
                "genre": ["economy", "politics"],
                "publisher": "nytimes"
            }
        }
        ```

        To use the same logical operator multiple times on the same level, logical operators can take a list of
        dictionaries as value.

        Example:

        ```python
        filters = {
            "$or": [
                {
                    "$and": {
                        "Type": "News Paper",
                        "Date": {
                            "$lt": "2019-01-01"
                        }
                    }
                },
                {
                    "$and": {
                        "Type": "Blog Post",
                        "Date": {
                            "$gte": "2019-01-01"
                        }
                    }
                }
            ]
        }
        ```

        :param filters: the filters to apply to the document list.
        :return: a list of Documents that match the given filters.
        """
        if filters:
            return [doc for doc in self.storage.values() if match(conditions=filters, document=doc)]
        return list(self.storage.values())

    def write_documents(self, documents: List[Document], duplicates: DuplicatePolicy = "fail") -> None:
        """
        Writes (or overwrites) documents into the store.

        :param documents: a list of documents.
        :param duplicates: documents with the same ID count as duplicates. When duplicates are met,
            the store can:
             - skip: keep the existing document and ignore the new one.
             - overwrite: remove the old document and write the new one.
             - fail: an error is raised
        :raises DuplicateError: Exception trigger on duplicate document if `duplicates="fail"`
        :return: None
        """
        if (
            not isinstance(documents, Iterable)
            or isinstance(documents, str)
            or any(not isinstance(doc, Document) for doc in documents)
        ):
            raise ValueError("Please provide a list of Documents.")

        for document in documents:
            if document.id in self.storage.keys():
                if duplicates == "fail":
                    raise DuplicateDocumentError(f"ID '{document.id}' already exists.")
                if duplicates == "skip":
                    logger.warning("ID '%s' already exists", document.id)
            self.storage[document.id] = document

    def delete_documents(self, document_ids: List[str]) -> None:
        """
        Deletes all documents with a matching document_ids from the document store.
        Fails with `MissingDocumentError` if no document with this id is present in the store.

        :param object_ids: the object_ids to delete
        """
        for doc_id in document_ids:
            if not doc_id in self.storage.keys():
                raise MissingDocumentError(f"ID '{doc_id}' not found, cannot delete it.")
            del self.storage[doc_id]

    @property
    def bm25_tokenization_regex(self):
        return self._tokenizer

    @bm25_tokenization_regex.setter
    def bm25_tokenization_regex(self, regex_string: str):
        self._tokenizer = re.compile(regex_string).findall

    @property
    def bm25_algorithm(self):
        return self._bm25_class

    @bm25_algorithm.setter
    def bm25_algorithm(self, algorithm: str):
        algorithm_class = getattr(rank_bm25, algorithm)
        if algorithm_class is None:
            raise ValueError(f"BM25 algorithm '{algorithm}' not found.")
        self._bm25_class = algorithm_class

    def bm25_retrieval(self, query: str, top_k: int = 10, scale_score: bool = True) -> List[Document]:
        """
        Retrieves documents that are most relevant to the query using BM25 algorithm.
        """

        all_documents = self.filter_documents(filters={"content_type": ["text", "table"]})
        lower_case_documents = []
        for doc in all_documents:
            if doc.content_type == "text":
                lower_case_documents.append(doc.content.lower())
            elif doc.content_type == "table":
                if isinstance(doc.content, pd.DataFrame):
                    lower_case_documents.append(doc.content.astype(str).to_csv(index=False).lower())

        tokenized_corpus = [
            self.bm25_tokenization_regex(doc)
            for doc in tqdm(lower_case_documents, unit=" docs", desc="Updating BM25 representation...")
        ]
        self.bm25 = self.bm25_algorithm(tokenized_corpus, **self.bm25_parameters)

        tokenized_query = self.bm25_tokenization_regex(query.lower())
        docs_scores = self.bm25.get_scores(tokenized_query)
        if scale_score is True:
            # scaling probability from BM25
            docs_scores = [float(expit(np.asarray(score / 8))) for score in docs_scores]
        top_docs_positions = np.argsort(docs_scores)[::-1][:top_k]

        return_documents = []
        for i in top_docs_positions:
            doc = all_documents[i]
            doc_as_dict = doc.to_dict()
            doc_as_dict["score"] = docs_scores[i]
            doc = Document(**doc_as_dict)
            return_document = copy.copy(doc)
            return_documents.append(return_document)
        return return_documents
