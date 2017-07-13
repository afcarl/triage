import pandas as pd
import copy
import random


def choose_highest_model_group_id(
    db_engine,
    model_group_ids,
    train_end_date,
    metric,
    metric_param
):
    return max(model_group_ids)


def choose_random_model_group_id(
    db_engine,
    model_group_ids,
    train_end_date,
    metric,
    metric_param
):
    return random.choice(model_group_ids)

SELECTION_RULES = {
    'highest_id': choose_highest_model_group_id,
    'random': choose_random_model_group_id,
}


def plot_best_dist(metric, metric_param, df_best_dist, **plt_format_args):
    """Generates a plot of the percentage of time that a model group is within X percentage points
       of the best-performing model group using a given metric. At each point in time that a set of
       model groups is evaluated, the performance of the best model is calculated and the difference
       in performace for all other models found relative to this. An (x,y) point for a given model 
       group on the plot generated by this method means that across all of those tets sets, the model
       from that model group performed within X percentage points of the best model in y% of the test
       sets. 
       The plot will contain a line for each model group in the ExperimentEvaluation object
       representing the cumulative percent of the time that the group is within Xpp of the best group
       for each value of X between 0 and 100. All groups ultimately reach (1,1) on this graph (as every
       model group must be within 100pp of the best model 100% of the time), and a model specification
       that always dominated the others in the experiment would start at (0,1) and remain at y=1
       across the graph.

    Arguments:
        metric (string) -- model evaluation metric, such as 'precision@'; if not specified the object's
                           default_metric will be used.
        metric_param (string) -- model evaluation metric parameter, such as '300_abs'; if not specified
                                 the object's default_metric_param will be used.
        **plt_format_args -- formatting arguments passed through to plot_cats()
    """

    cat_col = 'model_type'
    plt_title = 'Fraction of models X pp worse than best {} {}'.format(metric, metric_param)

    plot_cats(
        df_best_dist,
        'pct_diff',
        'pct_of_time',
        cat_col=cat_col,
        title=plt_title,
        x_label='decrease in {} from best model'.format(metric),
        y_label='fraction of models',
        x_ticks=np.arange(0,1.1,0.1),
        **plt_format_args
    )


def plot_all_best_dist(metrics_with_dfs):
    for metric in metrics_with_dfs:
        plot_best_dist(
            metric['metric'],
            metric['metric_param'],
            metric['distance_matrix']
        )

class ModelSelector(object):
    def __init__(self, db_engine, models_table):
        self.db_engine = db_engine
        self.models_table = models_table

    def _create_distance_table(self, table_name):
        self.db_engine.execute('''create table {} (
            model_group_id int,
            model_id int,
            train_end_time timestamp,
            metric text,
            parameter text,
            below_best float,
            below_best_next_time float
        )'''.format(table_name))

    def _populate_distance_table(self, model_group_ids, metrics, table_name):
        for metric in metrics:
            self.db_engine.execute('''
                insert into {new_table}
                WITH model_ranks AS (
                    SELECT
                        m.model_group_id,
                        m.model_id,
                        m.train_end_time,
                        ev.value,
                        row_number() OVER (
                            PARTITION BY m.train_end_time
                            ORDER BY ev.value DESC, RANDOM()
                        ) AS rank
                  FROM results.evaluations ev
                  JOIN results.{models_table} m USING(model_id)
                  JOIN results.model_groups mg USING(model_group_id)
                  WHERE ev.metric='{metric}' AND ev.parameter='{metric_param}'
                        AND m.model_group_id IN ({model_group_ids})
                ),
                model_tols AS (
                  SELECT train_end_time, model_group_id, model_id,
                         rank,
                         value,
                         first_value(value) over (
                            partition by train_end_time
                            order by rank ASC
                        ) AS best_val
                  FROM model_ranks
                ),
                current_best_vals as (
                    SELECT
                        model_group_id,
                        model_id,
                        train_end_time,
                        '{metric}',
                        '{metric_param}',
                        best_val - value below_best
                    FROM model_tols
                )
                select
                    current_best_vals.*,
                    first_value(below_best) over (
                        partition by model_group_id
                        order by train_end_time asc
                        rows between 1 following and unbounded following
                    ) below_best_next_time
                from current_best_vals
            '''.format(
                model_group_ids=','.join(map(str, model_group_ids)),
                models_table=self.models_table,
                metric=metric['metric'],
                metric_param=metric['param'],
                new_table=table_name
            ))

    def create_and_populate_distance_table(
        self,
        model_group_ids,
        metrics,
        distance_table
    ):
        self._create_distance_table(distance_table)
        self._populate_distance_table(model_group_ids, metrics, distance_table)

    def _hyperparam_sql(self, hyperparam):
        """Helper function to generate SQL snippet for pulling different
            types of model configuration
           and hyperparameter information out of the database.

        Arguments:
            hyperparam (string) -- name of the hyperparameter to query

        Returns: string SQL snippet for querying hyperparameter
        """

        if hyperparam is None:
            hyperparam_sql = 'NULL::VARCHAR(64) AS hyperparam,'
        elif hyperparam == 'feature_hash':
            hyperparam_sql = "md5(mg.feature_list::VARCHAR) AS hyperparam,"
        else:
            hyperparam_sql = "COALESCE(mg.model_parameters->>'{}', mg.model_config->>'{}') AS hyperparam,".format(hyperparam, hyperparam)

        return hyperparam_sql

    def get_best_dist(self, dist_table, metric, metric_param, max_below_best, model_type=None, hyperparam=None):
        """Fetch a best distance data frame from the distance table

        Arguments:
            metric (string) -- model evaluation metric, such as 'precision@'
            metric_param (string) -- model evaluation metric parameter,
                such as '300_abs'
            model_type (string) -- model type, such as
                sklearn.ensemble.RandomForestClassifier (may be None)
            hyperparam (string) -- model hyperparameter, such as
                max_depth (may be None)
        """

        # optionally filter down to a certain type of model
        # within the experiment, for instance,
        # to look at random forest-specific hyperparameters.
        if model_type is None:
            model_type_sql = ''
        else:
            model_type_sql = "AND mg.model_type='{}'".format(model_type)

        # grab sql snippet for querying hyperparameter from model_groups data
        hyperparam_sql = self._hyperparam_sql(hyperparam)

        sel_params = {
            'metric': metric,
            'metric_param': metric_param,
            'hyperparam_sql': hyperparam_sql,
            'model_type_sql': model_type_sql,
            'models_table': self.models_table,
            'distance_table': dist_table,
            'max_below_best': max_below_best,
        }

        sel = """
                with x_vals AS (
                  SELECT m.model_group_id, s.pct_diff
                  FROM
                  (
                  SELECT GENERATE_SERIES(0,100) / 100.0 AS pct_diff
                  ) s
                  CROSS JOIN
                  (
                  SELECT DISTINCT model_group_id FROM results.{models_table}
                  ) m
                )
                SELECT dist.model_group_id, model_type, {hyperparam_sql} pct_diff,
                       COUNT(*) AS num_models,
                       AVG(CASE WHEN below_best <= pct_diff THEN 1 ELSE 0 END) AS pct_of_time
                FROM {distance_table} dist
                JOIN results.model_groups mg USING(model_group_id)
                JOIN x_vals USING(model_group_id)
                WHERE
                    dist.metric='{metric}'
                    AND dist.parameter='{metric_param}'
                    and pct_diff <= {max_below_best}
                    and below_best <= {max_below_best}
                {model_type_sql}
                GROUP BY 1,2,3,4
            """.format(**sel_params)

        return pd.read_sql(sel, self.db_engine)

    def get_all_distance_matrices(self, dist_table, metrics):
        metrics_with_dfs = copy.deepcopy(metrics)
        for i, metric_config in enumerate(metrics):
            metrics_with_dfs[i]['distance_matrix'] = self.get_best_dist(
                dist_table=dist_table,
                metric=metric_config['metric'],
                metric_param=metric_config['metric_param'],
                max_below_best=metric_config['max_below_best'],
            )
        return metrics_with_dfs

    def calculate_regrets(
        self,
        distance_table,
        selection_rule,
        model_group_ids,
        train_end_dates,
        metric,
        metric_param
    ):
        regrets = []
        for train_end_date in train_end_dates:
            choice = selection_rule(
                self.db_engine,
                model_group_ids,
                train_end_date,
                metric,
                metric_param
            )
            regret_result = self.db_engine.execute('''
                select below_best_next_time
                from %s(distance_table)
                where model_group_id = %s(model_group_id)
                and train_end_date = %s(train_end_date)
                and metric = %s(metric)
                and parameter = %s(metric_param)
            ''',
                distance_table=distance_table,
                model_group_id=choice,
                train_end_date=train_end_date,
                metric=metric,
                metric_param=metric_param
            )
            regrets.append([row[0] for row in regret_result][0])
        return regrets