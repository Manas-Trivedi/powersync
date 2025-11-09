import numpy as np
import pandas as pd
from scipy.optimize import linprog, minimize
import cvxpy as cp

class StorageOptimizationEngine:
    """
    Real-time storage scheduling optimizer for batteries and pumped hydro.
    """

    def __init__(self, storage_config):
        self.config = storage_config
        # Battery specifications
        self.battery_capacity_mwh = storage_config['battery_capacity_mwh']
        self.battery_power_mw = storage_config['battery_power_mw']
        self.battery_efficiency = storage_config['battery_efficiency']
        self.battery_cycles_limit = storage_config['battery_cycle_life']

        # Pumped hydro specifications
        self.hydro_capacity_mwh = storage_config['hydro_capacity_mwh']
        self.hydro_power_mw = storage_config['hydro_power_mw']
        self.hydro_efficiency = storage_config['hydro_efficiency']

        # Degradation model
        self.degradation_cost_per_cycle = storage_config['degradation_cost']

        # State tracking
        self.battery_soc = storage_config['initial_battery_soc']
        self.hydro_soc = storage_config['initial_hydro_soc']
        self.cumulative_cycles = 0

    def calculate_degradation_cost(self, charge_mwh, discharge_mwh, storage_type='battery'):
        """
        Calculate battery degradation cost based on charge/discharge cycles.

        Degradation increases with:
        - Depth of Discharge (DoD)
        - State of Charge extremes
        - Temperature (approximated)
        """
        if storage_type == 'battery':
            capacity = self.battery_capacity_mwh
            # Effective cycle = (charge + discharge) / (2 × capacity)
            cycle_fraction = (charge_mwh + discharge_mwh) / (2 * capacity)

            # Degradation acceleration factors
            dod_factor = 1 + (cycle_fraction ** 2)  # Higher DoD = more degradation

            # Base cost per cycle
            base_cost = self.degradation_cost_per_cycle * cycle_fraction * dod_factor

            return base_cost
        else:  # pumped hydro - minimal degradation
            return 0.001 * (charge_mwh + discharge_mwh)

    def optimize_daily_schedule(self, price_forecast, generation_forecast,
                                 demand_forecast, reserve_requirement=0.05):
        """
        Optimize 24-hour storage schedule using CVXPY.

        Returns optimal charge/discharge schedule maximizing profit.
        """
        T = len(price_forecast)  # Time periods (typically 24 hours)

        # Decision variables
        # Battery
        P_bat_charge = cp.Variable(T, nonneg=True)
        P_bat_discharge = cp.Variable(T, nonneg=True)
        SoC_bat = cp.Variable(T+1, nonneg=True)
        u_bat_charge = cp.Variable(T, boolean=True)
        u_bat_discharge = cp.Variable(T, boolean=True)

        # Pumped Hydro
        P_hydro_charge = cp.Variable(T, nonneg=True)
        P_hydro_discharge = cp.Variable(T, nonneg=True)
        SoC_hydro = cp.Variable(T+1, nonneg=True)
        u_hydro_charge = cp.Variable(T, boolean=True)
        u_hydro_discharge = cp.Variable(T, boolean=True)

        # Objective: Maximize arbitrage profit minus degradation
        revenue = cp.sum([
            price_forecast[t] * (P_bat_discharge[t] + P_hydro_discharge[t])
            for t in range(T)
        ])

        cost = cp.sum([
            price_forecast[t] * (P_bat_charge[t] + P_hydro_charge[t])
            for t in range(T)
        ])

        # Simplified degradation (linear approximation)
        degradation = cp.sum([
            self.degradation_cost_per_cycle * (P_bat_charge[t] + P_bat_discharge[t]) /
            (2 * self.battery_capacity_mwh)
            for t in range(T)
        ])

        objective = cp.Maximize(revenue - cost - degradation)

        # Constraints
        constraints = []

        # Battery constraints
        for t in range(T):
            # Energy balance
            constraints.append(
                SoC_bat[t+1] == SoC_bat[t] +
                self.battery_efficiency * P_bat_charge[t] -
                P_bat_discharge[t] / self.battery_efficiency
            )

            # Power limits
            constraints.append(P_bat_charge[t] <= self.battery_power_mw * u_bat_charge[t])
            constraints.append(P_bat_discharge[t] <= self.battery_power_mw * u_bat_discharge[t])

            # Mutual exclusivity (can't charge and discharge simultaneously)
            constraints.append(u_bat_charge[t] + u_bat_discharge[t] <= 1)

            # SoC limits (20% to 90% for battery health)
            constraints.append(SoC_bat[t] >= 0.2 * self.battery_capacity_mwh)
            constraints.append(SoC_bat[t] <= 0.9 * self.battery_capacity_mwh)

        # Initial and final SoC
        constraints.append(SoC_bat[0] == self.battery_soc)
        # End-of-day SoC target (for next day continuity)
        constraints.append(SoC_bat[T] >= 0.4 * self.battery_capacity_mwh)

        # Pumped Hydro constraints
        for t in range(T):
            constraints.append(
                SoC_hydro[t+1] == SoC_hydro[t] +
                self.hydro_efficiency * P_hydro_charge[t] -
                P_hydro_discharge[t] / self.hydro_efficiency
            )

            constraints.append(P_hydro_charge[t] <= self.hydro_power_mw * u_hydro_charge[t])
            constraints.append(P_hydro_discharge[t] <= self.hydro_power_mw * u_hydro_discharge[t])
            constraints.append(u_hydro_charge[t] + u_hydro_discharge[t] <= 1)

            # Hydro SoC limits (10% to 95%)
            constraints.append(SoC_hydro[t] >= 0.1 * self.hydro_capacity_mwh)
            constraints.append(SoC_hydro[t] <= 0.95 * self.hydro_capacity_mwh)

        constraints.append(SoC_hydro[0] == self.hydro_soc)
        constraints.append(SoC_hydro[T] >= 0.3 * self.hydro_capacity_mwh)

        # Reserve requirement constraint
        for t in range(T):
            available_discharge = (
                (SoC_bat[t] - 0.2 * self.battery_capacity_mwh) / self.battery_efficiency +
                (SoC_hydro[t] - 0.1 * self.hydro_capacity_mwh) / self.hydro_efficiency
            )
            constraints.append(
                available_discharge >= reserve_requirement * generation_forecast[t]
            )

        # Formulate and solve problem
        problem = cp.Problem(objective, constraints)

        try:
            problem.solve(solver=cp.GUROBI, verbose=False)

            if problem.status == 'optimal':
                schedule = {
                    'battery_charge': P_bat_charge.value,
                    'battery_discharge': P_bat_discharge.value,
                    'battery_soc': SoC_bat.value,
                    'hydro_charge': P_hydro_charge.value,
                    'hydro_discharge': P_hydro_discharge.value,
                    'hydro_soc': SoC_hydro.value,
                    'total_profit': problem.value,
                    'status': 'optimal'
                }

                return schedule
            else:
                return {'status': 'infeasible', 'message': problem.status}

        except Exception as e:
            # Fallback to heuristic if optimization fails
            return self.heuristic_schedule(price_forecast, generation_forecast)

    def heuristic_schedule(self, price_forecast, generation_forecast):
        """
        Simple heuristic when optimization solver unavailable.
        Rule: Charge during lowest 30% price hours, discharge during highest 30% price hours.
        """
        T = len(price_forecast)
        price_array = np.array(price_forecast)

        # Find charging and discharging windows
        charge_threshold = np.percentile(price_array, 30)
        discharge_threshold = np.percentile(price_array, 70)

        battery_charge = np.zeros(T)
        battery_discharge = np.zeros(T)
        battery_soc = np.zeros(T+1)
        battery_soc[0] = self.battery_soc

        for t in range(T):
            if price_array[t] <= charge_threshold and battery_soc[t] < 0.9 * self.battery_capacity_mwh:
                # Charge at maximum rate
                max_charge = min(
                    self.battery_power_mw,
                    (0.9 * self.battery_capacity_mwh - battery_soc[t]) / self.battery_efficiency
                )
                battery_charge[t] = max_charge
                battery_soc[t+1] = battery_soc[t] + self.battery_efficiency * max_charge

            elif price_array[t] >= discharge_threshold and battery_soc[t] > 0.2 * self.battery_capacity_mwh:
                # Discharge at maximum rate
                max_discharge = min(
                    self.battery_power_mw,
                    (battery_soc[t] - 0.2 * self.battery_capacity_mwh) * self.battery_efficiency
                )
                battery_discharge[t] = max_discharge
                battery_soc[t+1] = battery_soc[t] - max_discharge / self.battery_efficiency

            else:
                battery_soc[t+1] = battery_soc[t]

        return {
            'battery_charge': battery_charge,
            'battery_discharge': battery_discharge,
            'battery_soc': battery_soc,
            'status': 'heuristic'
        }

    def execute_real_time_adjustment(self, current_soc, current_price,
                                     forecast_prices_next_6h, generation_surplus):
        """
        Make real-time adjustment to storage schedule based on current conditions.

        Used when actual prices/generation deviate significantly from forecast.
        """
        # Decision logic based on current vs. forecasted prices
        avg_forecast_price = np.mean(forecast_prices_next_6h)
        price_ratio = current_price / avg_forecast_price

        # SoC-based decision boundaries
        soc_pct = current_soc / self.battery_capacity_mwh

        # Decision matrix
        if generation_surplus > 0 and price_ratio < 0.85 and soc_pct < 0.85:
            # Surplus generation + low price + room to charge → CHARGE
            action = 'charge'
            power = min(self.battery_power_mw, generation_surplus)

        elif price_ratio > 1.15 and soc_pct > 0.25:
            # High price + sufficient charge → DISCHARGE
            action = 'discharge'
            power = min(
                self.battery_power_mw,
                (current_soc - 0.2 * self.battery_capacity_mwh) * self.battery_efficiency
            )

        else:
            # Hold current state
            action = 'hold'
            power = 0

        return {'action': action, 'power_mw': power, 'reason': f'Price ratio: {price_ratio:.2f}, SoC: {soc_pct:.1%}'}