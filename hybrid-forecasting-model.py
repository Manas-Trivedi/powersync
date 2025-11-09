import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.neural_network import MLPRegressor
import xgboost as xgb
from scipy.interpolate import interp1d
from datetime import datetime, timedelta

class WeatherGenerationForecaster:
    """
    Hybrid forecasting model combining physics-based calculations
    with ML error correction for solar and wind generation prediction.
    """

    def __init__(self, zone_config):
        self.zone_config = zone_config  # Capacity, turbine specs, panel specs

        # Ensemble ML models for error correction
        self.solar_ml_model = xgb.XGBRegressor(
            n_estimators=200,
            max_depth=8,
            learning_rate=0.05,
            subsample=0.8
        )

        self.wind_ml_model = GradientBoostingRegressor(
            n_estimators=300,
            max_depth=10,
            learning_rate=0.03,
            min_samples_split=20
        )

        # Uncertainty quantification models
        self.uncertainty_model = RandomForestRegressor(
            n_estimators=100,
            max_depth=6
        )

    def physics_solar_generation(self, irradiance, temperature, humidity,
                                  dust_factor, panel_efficiency):
        """
        Calculate solar generation using physics-based model.

        Parameters:
        - irradiance: W/m² (from satellite/forecast)
        - temperature: °C (ambient)
        - humidity: % (affects transmittance)
        - dust_factor: 0-1 (accumulated dust impact)
        - panel_efficiency: Base efficiency at STC
        """
        # Temperature derating: -0.45% per °C above 25°C
        temp_coefficient = -0.0045
        temp_derating = 1 + temp_coefficient * (temperature - 25)

        # Dust accumulation impact
        dust_derating = 1 - (dust_factor * 0.25)  # Max 25% loss

        # Humidity transmittance effect (approximation)
        humidity_factor = 1 - (humidity / 100) * 0.08

        # Standard Test Conditions: 1000 W/m², 25°C
        stc_irradiance = 1000

        # Power calculation
        actual_efficiency = (panel_efficiency * temp_derating *
                           dust_derating * humidity_factor)

        power_ratio = irradiance / stc_irradiance
        generation_mw = (self.zone_config['solar_capacity_mw'] *
                        power_ratio * actual_efficiency)

        return max(0, generation_mw)

    def physics_wind_generation(self, wind_speed, air_density, turbine_curve):
        """
        Calculate wind generation using power curve and air density correction.

        Parameters:
        - wind_speed: m/s at hub height
        - air_density: kg/m³ (temperature/pressure dependent)
        - turbine_curve: Power curve lookup function
        """
        # Standard air density at sea level, 15°C
        standard_density = 1.225

        # Density correction factor
        density_correction = air_density / standard_density

        # Turbine specifications
        cut_in_speed = 3.0  # m/s
        cut_out_speed = 25.0  # m/s
        rated_speed = 12.0  # m/s

        # Check operational range
        if wind_speed < cut_in_speed or wind_speed > cut_out_speed:
            return 0

        # Get power from turbine curve (typically cubic below rated)
        if wind_speed <= rated_speed:
            # Simplified: P ∝ v³ (actual curves are manufacturer-specific)
            power_fraction = (wind_speed / rated_speed) ** 3
        else:
            power_fraction = 1.0  # Rated power

        # Apply density correction
        corrected_power = power_fraction * density_correction

        generation_mw = (self.zone_config['wind_capacity_mw'] *
                        corrected_power)

        return max(0, min(generation_mw, self.zone_config['wind_capacity_mw']))

    def calculate_air_density(self, temperature, pressure, humidity):
        """Calculate air density using ideal gas law with humidity correction."""
        # Temperature in Kelvin
        T = temperature + 273.15

        # Pressure in Pa
        P = pressure * 100  # Convert hPa to Pa

        # Water vapor pressure (simplified Magnus formula)
        vapor_pressure = humidity / 100 * 611.2 * np.exp(
            17.67 * temperature / (temperature + 243.5)
        )

        # Dry air gas constant
        R_d = 287.05  # J/(kg·K)
        # Water vapor gas constant
        R_v = 461.495  # J/(kg·K)

        # Air density with humidity
        density = (P - vapor_pressure) / (R_d * T) + vapor_pressure / (R_v * T)

        return density

    def prepare_features(self, weather_df, historical_gen_df):
        """
        Prepare features for ML error correction model.
        """
        features = weather_df.copy()

        # Temporal features
        features['hour'] = features['timestamp'].dt.hour
        features['day_of_year'] = features['timestamp'].dt.dayofyear
        features['month'] = features['timestamp'].dt.month
        features['day_of_week'] = features['timestamp'].dt.dayofweek

        # Cyclical encoding for time features
        features['hour_sin'] = np.sin(2 * np.pi * features['hour'] / 24)
        features['hour_cos'] = np.cos(2 * np.pi * features['hour'] / 24)
        features['doy_sin'] = np.sin(2 * np.pi * features['day_of_year'] / 365)
        features['doy_cos'] = np.cos(2 * np.pi * features['day_of_year'] / 365)

        # Weather derivative features
        features['temp_change_3h'] = features['temperature'].diff(3).fillna(0)
        features['wind_change_1h'] = features['wind_speed'].diff(1).fillna(0)
        features['cloud_cover_rate'] = features['cloud_cover_pct'].diff(1).fillna(0)

        # Historical generation patterns (lagged features)
        if historical_gen_df is not None:
            features['gen_same_hour_yesterday'] = features['timestamp'].apply(
                lambda x: self._get_historical_gen(historical_gen_df, x, days_back=1)
            )
            features['gen_same_hour_last_week'] = features['timestamp'].apply(
                lambda x: self._get_historical_gen(historical_gen_df, x, days_back=7)
            )

        # Interaction features
        features['temp_humidity_interaction'] = (features['temperature'] *
                                                 features['humidity'] / 100)
        features['wind_density_interaction'] = features['wind_speed'] * features['air_density']

        return features

    def _get_historical_gen(self, historical_df, timestamp, days_back=1):
        """Helper to get generation from same hour in previous days."""
        target_time = timestamp - timedelta(days=days_back)
        matches = historical_df[
            (historical_df['timestamp'] >= target_time - timedelta(minutes=30)) &
            (historical_df['timestamp'] <= target_time + timedelta(minutes=30))
        ]
        return matches['generation_mw'].mean() if len(matches) > 0 else 0

    def train_solar_model(self, training_data):
        """
        Train ML model to correct physics-based solar forecast errors.
        """
        # Calculate physics-based predictions
        training_data['physics_prediction'] = training_data.apply(
            lambda row: self.physics_solar_generation(
                row['irradiance'], row['temperature'], row['humidity'],
                row['dust_factor'], self.zone_config['solar_efficiency']
            ), axis=1
        )

        # Calculate error to be corrected
        training_data['physics_error'] = (training_data['actual_generation'] -
                                          training_data['physics_prediction'])

        # Prepare features
        features = self.prepare_features(training_data, None)
        feature_cols = [col for col in features.columns
                       if col not in ['timestamp', 'actual_generation', 'physics_error']]

        X = features[feature_cols]
        y = training_data['physics_error']

        # Train model
        self.solar_ml_model.fit(X, y)

        # Train uncertainty model on absolute errors
        self.uncertainty_model.fit(X, np.abs(y))

        return self

    def train_wind_model(self, training_data):
        """
        Train ML model to correct physics-based wind forecast errors.
        """
        # Calculate air density
        training_data['air_density'] = training_data.apply(
            lambda row: self.calculate_air_density(
                row['temperature'], row['pressure'], row['humidity']
            ), axis=1
        )

        # Calculate physics-based predictions
        training_data['physics_prediction'] = training_data.apply(
            lambda row: self.physics_wind_generation(
                row['wind_speed'], row['air_density'], None
            ), axis=1
        )

        # Calculate error
        training_data['physics_error'] = (training_data['actual_generation'] -
                                          training_data['physics_prediction'])

        # Prepare features
        features = self.prepare_features(training_data, None)
        feature_cols = [col for col in features.columns
                       if col not in ['timestamp', 'actual_generation', 'physics_error']]

        X = features[feature_cols]
        y = training_data['physics_error']

        # Train model
        self.wind_ml_model.fit(X, y)

        return self

    def forecast_generation(self, weather_forecast_df, energy_type='solar'):
        """
        Generate hybrid physics-ML forecast with uncertainty bands.

        Returns:
        - DataFrame with predicted generation and uncertainty bounds
        """
        forecast = weather_forecast_df.copy()

        # Calculate physics-based prediction
        if energy_type == 'solar':
            forecast['physics_prediction'] = forecast.apply(
                lambda row: self.physics_solar_generation(
                    row['irradiance'], row['temperature'], row['humidity'],
                    row.get('dust_factor', 0.1),
                    self.zone_config['solar_efficiency']
                ), axis=1
            )
            ml_model = self.solar_ml_model
        else:  # wind
            forecast['air_density'] = forecast.apply(
                lambda row: self.calculate_air_density(
                    row['temperature'], row['pressure'], row['humidity']
                ), axis=1
            )
            forecast['physics_prediction'] = forecast.apply(
                lambda row: self.physics_wind_generation(
                    row['wind_speed'], row['air_density'], None
                ), axis=1
            )
            ml_model = self.wind_ml_model

        # Prepare features for ML correction
        features = self.prepare_features(forecast, None)
        feature_cols = [col for col in features.columns
                       if col not in ['timestamp', 'physics_prediction']]

        X = features[feature_cols]

        # Predict error correction
        error_correction = ml_model.predict(X)

        # Predict uncertainty
        uncertainty = self.uncertainty_model.predict(X)

        # Final prediction
        forecast['predicted_generation'] = (forecast['physics_prediction'] +
                                           error_correction)
        forecast['predicted_generation'] = forecast['predicted_generation'].clip(lower=0)

        # Uncertainty bounds (95% confidence interval, ±2 std)
        forecast['lower_bound'] = (forecast['predicted_generation'] -
                                   2 * uncertainty).clip(lower=0)
        forecast['upper_bound'] = (forecast['predicted_generation'] +
                                   2 * uncertainty)

        # Capacity constraints
        max_capacity = (self.zone_config['solar_capacity_mw'] if energy_type == 'solar'
                       else self.zone_config['wind_capacity_mw'])
        forecast['upper_bound'] = forecast['upper_bound'].clip(upper=max_capacity)

        return forecast[['timestamp', 'predicted_generation',
                        'lower_bound', 'upper_bound']]

    def ensemble_zone_forecast(self, weather_forecasts_by_zone):
        """
        Generate forecasts for all zones and aggregate.

        Parameters:
        - weather_forecasts_by_zone: Dict of {zone_name: weather_df}

        Returns:
        - Dict of {zone_name: forecast_df} and aggregated total
        """
        zone_forecasts = {}

        for zone, weather_df in weather_forecasts_by_zone.items():
            # Determine energy type from zone config
            if self.zone_config[zone]['solar_capacity_mw'] > 0:
                solar_forecast = self.forecast_generation(weather_df, 'solar')
                zone_forecasts[f"{zone}_solar"] = solar_forecast

            if self.zone_config[zone]['wind_capacity_mw'] > 0:
                wind_forecast = self.forecast_generation(weather_df, 'wind')
                zone_forecasts[f"{zone}_wind"] = wind_forecast

        return zone_forecasts