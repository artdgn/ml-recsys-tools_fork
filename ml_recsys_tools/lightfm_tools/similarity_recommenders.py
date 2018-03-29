from copy import deepcopy
from functools import partial

import numpy as np
from sklearn.preprocessing import normalize

from ml_recsys_tools.lightfm_tools.recommender_base import BaseDFSparseRecommender
from ml_recsys_tools.utils.similarity import top_N_sorted_on_sparse, custom_row_func_on_sparse
from ml_recsys_tools.utils.debug import log_time_and_shape


@log_time_and_shape
def interactions_mat_to_cooccurrence_mat(
        obs_mat, normalize_items=True, degree=1, base_min_cooccurrence=1,
        prune_ratio=0.5, decay=0.3, min_cooccurrence=3, trans_func='ones'):
    def prune_mat(m, ratio=0.0, cutoff=0):
        if (ratio == 0.0 and cutoff == 0) or (not len(m.data)):
            return m
        else:
            if ratio > 0.0:
                if len(m.data) > 50000:
                    data_sample = np.random.choice(
                        m.data, min(len(m.data), 10000), replace=False)
                else:
                    data_sample = m.data
                cutoff = max(cutoff, np.percentile(data_sample, int(100 * ratio)))

            m.data[m.data < cutoff] *= 0
            m.eliminate_zeros()
        return m

    def first_degree_cooccurrence(mat, min_cooc=1):

        if trans_func == 'ones':
            # binarize interaction
            mat.data = np.ones(mat.data.shape)

        elif trans_func == 'log':
            mat.data = np.log10(mat.data + 1)

        elif trans_func == 'none':
            pass

        else:
            raise ValueError('Unknown trans_func: %s' % trans_func)

        # 1st degree interaction matrix
        cooc_mat = mat.T * mat
        # remove self similarities
        cooc_mat.setdiag(0)
        # maybe less memory
        cooc_mat = cooc_mat.astype(np.float32)

        if min_cooc > 1:
            # threshold interactions
            cooc_mat = prune_mat(cooc_mat, 0.0, min_cooc)

        return cooc_mat

    cooc_mat_base = first_degree_cooccurrence(obs_mat, base_min_cooccurrence)

    if degree > 1:

        # keep weight constant
        total_weight = np.sum(cooc_mat_base.data)

        higher_deg_cooc = prune_mat(cooc_mat_base.copy(), prune_ratio, min_cooccurrence)

        for i in range(degree - 1):
            higher_deg_cooc += \
                decay ** (i + 1) * \
                higher_deg_cooc * \
                higher_deg_cooc

            # remove self similarities
            higher_deg_cooc.setdiag(0)

        higher_deg_cooc.data *= total_weight / (np.sum(higher_deg_cooc.data) + 1)  # avoid divide by 0

        cooc_mat_base += higher_deg_cooc

    # mormalization
    if normalize_items:
        cooc_mat_base = normalize(cooc_mat_base, norm='l1', axis=1)

    return cooc_mat_base


class BaseSimilarityRecommeder(BaseDFSparseRecommender):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.similarity_mat = None

    def _prep_for_fit(self, train_obs, **fit_params):
        self._set_fit_params(fit_params)
        self.sparse_mat_builder = train_obs.get_sparse_matrix_helper()
        self.train_df = train_obs.df_obs
        self.train_mat = self.sparse_mat_builder.build_sparse_interaction_matrix(self.train_df)

    def _check_no_negatives(self):
        # prevents negative scores from being smaller than sparse zeros (e.g. for euclidean similarity)
        if len(self.similarity_mat.data) and np.min(self.similarity_mat.data) < 0.01:
            self.similarity_mat.data += np.abs(np.min(self.similarity_mat.data) - 0.01)

    def _recommend_for_item_inds(self, item_inds, *args, n_rec_unfilt=100, remove_self=True, **kwargs):

        sub_mat = self.similarity_mat[item_inds, :]

        sum_simils = np.array(np.sum(sub_mat, axis=0)).ravel()

        if remove_self:
            sum_simils[item_inds] *= 0

        n_rec = min(n_rec_unfilt, len(sum_simils))

        i_part = np.argpartition(sum_simils, -n_rec)[-n_rec:]
        i_sort = i_part[np.argsort(-sum_simils[i_part])[:n_rec]]

        return i_sort, sum_simils[i_sort]

    def recommend_for_interaction_history(self, interactions_ids, n_rec):
        interactions_inds = self.sparse_mat_builder.iid_encoder.transform(interactions_ids)
        rec_ids, rec_scores = self._recommend_for_item_inds(interactions_inds, n_rec_unfilt=n_rec)
        return self.sparse_mat_builder.iid_encoder.inverse_transform(rec_ids), rec_scores

    @log_time_and_shape
    def _get_recommendations_flat_unfilt(
            self, user_ids, n_rec_unfilt=100, pbar=None, **kwargs):

        self._check_no_negatives()

        top_simil_for_users = partial(self._recommend_for_item_inds,
                                      n_rec_unfilt=n_rec_unfilt, remove_self=False)

        best_ids, best_scores = custom_row_func_on_sparse(
            row_func=top_simil_for_users,
            ids=user_ids,
            source_encoder=self.sparse_mat_builder.uid_encoder,
            target_encoder=self.sparse_mat_builder.iid_encoder,
            sparse_mat=self.train_mat,
            pbar=pbar,
            chunksize=500,
        )

        return self._format_results_df(
            source_vec=user_ids, target_ids_mat=best_ids, scores_mat=best_scores,
            results_format='recommendations_flat')

    @log_time_and_shape
    def get_similar_items(self, itemids, N=10, results_format='lists', pbar=None, **kwargs):

        self._check_no_negatives()

        best_ids, best_scores = top_N_sorted_on_sparse(
            ids=itemids,
            encoder=self.sparse_mat_builder.iid_encoder,
            sparse_mat=self.similarity_mat,
            n_top=N,
            pbar=pbar
        )

        simil_df = self._format_results_df(
            itemids, target_ids_mat=best_ids,
            scores_mat=best_scores, results_format='similarities_' + results_format)
        return simil_df


class ItemCoocRecommender(BaseSimilarityRecommeder):

    def __init__(self, degree=1, normalize_items=True,
                 prune_ratio=0.0, decay=0.5, min_cooccurrence=1,
                 base_min_cooccurrence=1, trans_func='ones', *args, **kwargs):
        super().__init__(
            *args,
            fit_params=dict(
                normalize_items=normalize_items,
                degree=degree,
                prune_ratio=prune_ratio,
                decay=decay,
                min_cooccurrence=min_cooccurrence,
                base_min_cooccurrence=base_min_cooccurrence,
                trans_func=trans_func,
            ),
            **kwargs)

    @log_time_and_shape
    def fit(self, train_obs, **fit_params):
        self._prep_for_fit(train_obs, **fit_params)
        self.similarity_mat = interactions_mat_to_cooccurrence_mat(
            self.train_mat, **self.fit_params)
        self.similarity_mat += self.similarity_mat.T

    def set_params(self, **params):
        """
        this is for skopt / sklearn compatibility
        """
        params = self._pop_set_dict(
            self.fit_params,
            params,
            ['degree', 'normalize_items', 'prune_ratio',
             'decay', 'min_cooccurrence', 'base_min_cooccurrence',
             'trans_func'])

        super().set_params(**params)


class UserCoocRecommender(ItemCoocRecommender):

    @log_time_and_shape
    def fit(self, train_obs, **fit_params):
        self._prep_for_fit(train_obs, **fit_params)
        self.similarity_mat = interactions_mat_to_cooccurrence_mat(
            self.train_mat.T, **self.fit_params)

    def recommend_for_interaction_history(self, interactions_ids, n_rec):
        raise NotImplementedError

    @log_time_and_shape
    def _get_recommendations_flat_unfilt(
            self, user_ids, n_rec_unfilt=100, pbar=None, **kwargs):
        def row_func(user_inds, row_data):
            sub_mat = self.train_mat[user_inds, :]
            sub_mat.sort_indices()
            for i, r in enumerate(row_data):
                sub_mat.data[sub_mat.indptr[i]:sub_mat.indptr[i + 1]] *= r

            sum_weight_occurs = np.array(np.sum(sub_mat.tocsr(), axis=0)).ravel()

            i_part = np.argpartition(sum_weight_occurs, -n_rec_unfilt)[-n_rec_unfilt:]
            i_sort = i_part[np.argsort(-sum_weight_occurs[i_part])[:n_rec_unfilt]]

            return i_sort, sum_weight_occurs[i_sort]

        best_ids, best_scores = custom_row_func_on_sparse(
            row_func=row_func,
            ids=user_ids,
            source_encoder=self.sparse_mat_builder.uid_encoder,
            target_encoder=self.sparse_mat_builder.iid_encoder,
            sparse_mat=self.similarity_mat,
            pbar=pbar
        )

        return self._format_results_df(
            source_vec=user_ids, target_ids_mat=best_ids, scores_mat=best_scores,
            results_format='recommendations_flat')

    def get_similar_items(self, itemids, N=10, results_format='lists', pbar=None, **kwargs):
        raise NotImplementedError


class SimilarityDFRecommender(BaseSimilarityRecommeder):

    def get_similarity_builder(self):
        # this is hacky, but I want to make sure this recommender is even useful first
        # should be some other class' method
        simil_mat_builder = deepcopy(self.sparse_mat_builder)
        simil_mat_builder.uid_source_col = self._item_col_simil
        simil_mat_builder.iid_source_col = self._item_col
        simil_mat_builder.rating_source_col = self._prediction_col
        simil_mat_builder.n_rows = simil_mat_builder.n_cols
        simil_mat_builder.uid_encoder = simil_mat_builder.iid_encoder
        return simil_mat_builder

    def _prep_for_fit(self, train_obs, **fit_params):
        super()._prep_for_fit(train_obs, **fit_params)
        self.similarity_mat_builder = self.get_similarity_builder()

    @log_time_and_shape
    def fit(self, train_obs, simil_df_flat, **fit_params):
        self._prep_for_fit(train_obs, **fit_params)
        self.similarity_mat = self.similarity_mat_builder. \
            build_sparse_interaction_matrix(simil_df_flat)
        self.similarity_mat += self.similarity_mat.T

    def continue_fit(self, simil_df_flat):
        partial_similarity_mat = self.similarity_mat_builder. \
            build_sparse_interaction_matrix(simil_df_flat)
        self.similarity_mat += partial_similarity_mat + partial_similarity_mat.T