import pandas as pd
import numpy as np
from prophet import Prophet
from sklearn.preprocessing import StandardScaler
import xgboost as xgb
from scipy.signal import find_peaks

class RegionalDemandForecaster:
    """
    Multi-zone demand forecasting with industrial load pattern recognition.
    """

    def __init__(self, zones):
        self.zones = zones
        self.models = {}
        self.scalers = {}
        self.load_profiles = {}

        for zone in zones:
            # Prophet for trend and seasonality
            self.models[f"{zone}_prophet"] = Prophet(
                yearly_seasonality=True, # type: ignore
                weekly_seasonality=True, # type: ignore
                daily_seasonality=True,  # type: ignore
                holidays=self._get_indian_holidays(),
                changepoint_prior_scale=0.08
            )

            # XGBoost for residual prediction
            self.models[f"{zone}_xgb"] = xgb.XGBRegressor(
                n_estimators=250,
                max_depth=9,
                learning_rate=0.04,
                subsample=0.85,
                colsample_bytree=0.85
            )

            self.scalers[zone] = StandardScaler()

    def _get_indian_holidays(self):
        """
        Define Indian national and regional holidays affecting demand.
        """
        holidays = pd.DataFrame({
            'holiday': ['Diwali', 'Holi', 'Eid', 'Republic Day', 'Independence Day',
                       'Dussehra', 'Christmas', 'Pongal', 'Onam', 'Durga Puja'],
            'ds': pd.to_datetime([
                '2024-11-01', '2024-03-25', '2024-04-11', '2024-01-26', '2024-08-15',
                '2024-10-12', '2024-12-25', '2024-01-15', '2024-09-15', '2024-10-10'
            ]),
            'lower_window': -1,
            'upper_window': 1,
        })
        return holidays

    def extract_load_patterns(self, historical_demand_df, zone):
        """
        Identify recurring industrial and commercial load patterns.
        """
        zone_data = historical_demand_df[historical_demand_df['zone'] == zone].copy()

        # Separate weekday vs weekend patterns
        zone_data['is_weekend'] = zone_data['timestamp'].dt.dayofweek >= 5

        weekday_pattern = zone_data[~zone_data['is_weekend']].groupby(
            zone_data['timestamp'].dt.hour
        )['demand_mw'].agg(['mean', 'std', 'quantile'])

        weekend_pattern = zone_data[zone_data['is_weekend']].groupby(
            zone_data['timestamp'].dt.hour
        )['demand_mw'].agg(['mean', 'std', 'quantile'])

        # Identify industrial peaks (sharp regular increases)
        hourly_avg = zone_data.groupby(zone_data['timestamp'].dt.hour)['demand_mw'].mean()
        peaks, properties = find_peaks(hourly_avg.values, prominence=50)

        # Store patterns
        self.load_profiles[zone] = {
            'weekday_hourly': weekday_pattern,
            'weekend_hourly': weekend_pattern,
            'industrial_peaks': peaks,
            'base_load': hourly_avg.min(),
            'peak_load': hourly_avg.max(),
            'peak_to_base_ratio': hourly_avg.max() / hourly_avg.min()
        }

        return self.load_profiles[zone]

    def engineer_features(self, df, zone):
        """
        Create comprehensive feature set for demand prediction.
        """
        features = df.copy()

        # Temporal features
        features['hour'] = features['timestamp'].dt.hour
        features['day_of_week'] = features['timestamp'].dt.dayofweek
        features['day_of_month'] = features['timestamp'].dt.day
        features['month'] = features['timestamp'].dt.month
        features['quarter'] = features['timestamp'].dt.quarter
        features['week_of_year'] = features['timestamp'].dt.isocalendar().week

        # Cyclical encoding
        features['hour_sin'] = np.sin(2 * np.pi * features['hour'] / 24)
        features['hour_cos'] = np.cos(2 * np.pi * features['hour'] / 24)
        features['dow_sin'] = np.sin(2 * np.pi * features['day_of_week'] / 7)
        features['dow_cos'] = np.cos(2 * np.pi * features['day_of_week'] / 7)
        features['month_sin'] = np.sin(2 * np.pi * features['month'] / 12)
        features['month_cos'] = np.cos(2 * np.pi * features['month'] / 12)

        # Binary indicators
        features['is_weekend'] = (features['day_of_week'] >= 5).astype(int)
        features['is_holiday'] = features['timestamp'].dt.date.isin(
            self._get_indian_holidays()['ds'].dt.date
        ).astype(int)
        features['is_summer'] = features['month'].isin([4, 5, 6]).astype(int)
        features['is_monsoon'] = features['month'].isin([6, 7, 8, 9]).astype(int)
        features['is_winter'] = features['month'].isin([11, 12, 1, 2]).astype(int)

        # Time-of-day indicators
        features['is_morning_peak'] = features['hour'].isin([8, 9, 10, 11]).astype(int)
        features['is_evening_peak'] = features['hour'].isin([18, 19, 20, 21]).astype(int)
        features['is_night'] = features['hour'].isin(range(0, 6)).astype(int)

        # Weather impact features
        if 'temperature' in features.columns:
            features['cooling_degree_hours'] = np.maximum(features['temperature'] - 24, 0)
            features['heating_degree_hours'] = np.maximum(18 - features['temperature'], 0)

        # Zone-specific industrial pattern features
        if zone in self.load_profiles:
            profile = self.load_profiles[zone]
            features['is_industrial_peak'] = features['hour'].isin(
                profile['industrial_peaks']
            ).astype(int)
            features['hour_base_ratio'] = features['hour'].map(
                lambda h: profile['weekday_hourly'].loc[h, 'mean'] / profile['base_load']
                if h in profile['weekday_hourly'].index else 1.0
            )

        # Lag features
        for lag in [1, 24, 168]:  # 1hr, 1day, 1week
            features[f'demand_lag_{lag}h'] = features['demand_mw'].shift(lag).fillna(
                features['demand_mw'].mean()
            )

        # Rolling statistics
        for window in [3, 6, 24]:
            features[f'demand_rolling_mean_{window}h'] = (
                features['demand_mw'].rolling(window=window).mean().fillna(
                    features['demand_mw'].mean()
                )
            )
            features[f'demand_rolling_std_{window}h'] = (
                features['demand_mw'].rolling(window=window).std().fillna(0)
            )

        return features

    def train_zone_model(self, historical_df, zone):
        """
        Train hybrid Prophet + XGBoost model for a specific zone.
        """
        zone_data = historical_df[historical_df['zone'] == zone].copy()

        # Extract load patterns first
        self.extract_load_patterns(historical_df, zone)

        # Prepare Prophet data
        prophet_df = zone_data[['timestamp', 'demand_mw']].rename(
            columns={'timestamp': 'ds', 'demand_mw': 'y'}
        )

        # Add regressors if available
        if 'temperature' in zone_data.columns:
            prophet_df['temperature'] = zone_data['temperature']
            self.models[f"{zone}_prophet"].add_regressor('temperature')

        if 'is_holiday' in zone_data.columns:
            prophet_df['is_holiday'] = zone_data['is_holiday']
            self.models[f"{zone}_prophet"].add_regressor('is_holiday')

        # Train Prophet
        self.models[f"{zone}_prophet"].fit(prophet_df)

        # Get Prophet predictions on training data
        prophet_predictions = self.models[f"{zone}_prophet"].predict(prophet_df)
        zone_data['prophet_prediction'] = prophet_predictions['yhat'].values

        # Calculate residuals
        zone_data['prophet_residual'] = (zone_data['demand_mw'] -
                                          zone_data['prophet_prediction'])

        # Engineer features for XGBoost
        features = self.engineer_features(zone_data, zone)

        # Select feature columns
        feature_cols = [col for col in features.columns
                       if col not in ['timestamp', 'demand_mw', 'zone',
                                     'prophet_prediction', 'prophet_residual']]

        X = features[feature_cols]
        y = zone_data['prophet_residual']

        # Scale features
        X_scaled = self.scalers[zone].fit_transform(X)

        # Train XGBoost on residuals
        self.models[f"{zone}_xgb"].fit(X_scaled, y)

        return self

    def predict_zone_demand(self, forecast_df, zone):
        """
        Generate demand forecast for a specific zone.
        """
        # Prophet prediction
        prophet_future = forecast_df[['timestamp']].rename(columns={'timestamp': 'ds'})

        if 'temperature' in forecast_df.columns:
            prophet_future['temperature'] = forecast_df['temperature']
        if 'is_holiday' in forecast_df.columns:
            prophet_future['is_holiday'] = forecast_df['is_holiday']

        prophet_forecast = self.models[f"{zone}_prophet"].predict(prophet_future)
        prophet_predictions = prophet_forecast['yhat'].values

        # Prepare features for XGBoost
        forecast_with_prophet = forecast_df.copy()
        forecast_with_prophet['prophet_prediction'] = prophet_predictions
        forecast_with_prophet['demand_mw'] = prophet_predictions  # Placeholder for feature engineering

        features = self.engineer_features(forecast_with_prophet, zone)

        feature_cols = [col for col in features.columns
                       if col not in ['timestamp', 'demand_mw', 'zone',
                                     'prophet_prediction', 'prophet_residual']]

        X = features[feature_cols]
        X_scaled = self.scalers[zone].transform(X)

        # XGBoost residual prediction
        residual_predictions = self.models[f"{zone}_xgb"].predict(X_scaled)

        # Combine predictions
        final_predictions = prophet_predictions + residual_predictions
        final_predictions = np.maximum(final_predictions, 0)  # Non-negative constraint

        # Uncertainty from Prophet
        prediction_interval = prophet_forecast[['yhat_lower', 'yhat_upper']].values

        result_df = pd.DataFrame({
            'timestamp': forecast_df['timestamp'],
            'zone': zone,
            'predicted_demand_mw': final_predictions,
            'lower_bound': prediction_interval[:, 0],
            'upper_bound': prediction_interval[:, 1],
            'prophet_component': prophet_predictions,
            'ml_correction': residual_predictions
        })

        return result_df

    def forecast_all_zones(self, forecast_df):
        """
        Generate forecasts for all zones.
        """
        all_forecasts = []

        for zone in self.zones:
            zone_forecast = self.predict_zone_demand(forecast_df, zone)
            all_forecasts.append(zone_forecast)

        combined = pd.concat(all_forecasts, ignore_index=True)

        # Add aggregated total
        total_by_hour = combined.groupby('timestamp').agg({
            'predicted_demand_mw': 'sum',
            'lower_bound': 'sum',
            'upper_bound': 'sum'
        }).reset_index()

        total_by_hour['zone'] = 'TOTAL'

        return combined, total_by_hour