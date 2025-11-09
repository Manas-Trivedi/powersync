import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque
import random

class BiddingDQN(nn.Module):
    """
    Deep Q-Network for learning optimal bidding strategies.
    """

    def __init__(self, state_dim, action_dim):
        super(BiddingDQN, self).__init__()
        self.fc1 = nn.Linear(state_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 128)
        self.fc4 = nn.Linear(128, action_dim)

        self.dropout = nn.Dropout(0.2)

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = self.dropout(x)
        x = torch.relu(self.fc2(x))
        x = self.dropout(x)
        x = torch.relu(self.fc3(x))
        x = self.fc4(x)
        return x

class DynamicBiddingAgent:
    """
    Reinforcement learning agent for IEX market bidding optimization.
    """

    def __init__(self, state_dim=45, volume_actions=20, price_actions=15):
        self.state_dim = state_dim
        self.volume_actions = volume_actions
        self.price_actions = price_actions
        self.action_dim = volume_actions * price_actions  # Combined action space

        # Neural networks
        self.policy_net = BiddingDQN(state_dim, self.action_dim)
        self.target_net = BiddingDQN(state_dim, self.action_dim)
        self.target_net.load_state_dict(self.policy_net.state_dict())

        # Training parameters
        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=0.0001)
        self.memory = deque(maxlen=50000)
        self.batch_size = 128
        self.gamma = 0.99  # Discount factor
        self.epsilon = 1.0  # Exploration rate
        self.epsilon_decay = 0.995
        self.epsilon_min = 0.05

        # Action space definitions
        self.volume_levels = np.linspace(0, 12000, volume_actions)  # 0 to 12 GW in MW
        self.price_levels = np.linspace(2.0, 10.0, price_actions)  # ₹2 to ₹10 per kWh

    def encode_state(self, generation_forecast, demand_forecast, storage_soc,
                     price_history, current_hour, day_of_week, month):
        """
        Encode market state into feature vector.
        """
        state = []

        # Generation forecast (next 6 hours)
        state.extend(generation_forecast[:6])

        # Demand forecast (next 6 hours, aggregated)
        state.extend([demand_forecast[i:i+1].sum() for i in range(6)])

        # Storage state (normalized)
        state.append(storage_soc['battery'] / 420)  # Normalize by capacity
        state.append(storage_soc['hydro'] / 2800)

        # Price history (same hour, last 7 days)
        state.extend(price_history[-7:])

        # Temporal features (cyclical encoding)
        state.append(np.sin(2 * np.pi * current_hour / 24))
        state.append(np.cos(2 * np.pi * current_hour / 24))
        state.append(np.sin(2 * np.pi * day_of_week / 7))
        state.append(np.cos(2 * np.pi * day_of_week / 7))
        state.append(np.sin(2 * np.pi * month / 12))
        state.append(np.cos(2 * np.pi * month / 12))

        # Market indicators
        price_trend = (price_history[-1] - price_history[-7]) / price_history[-7]
        state.append(price_trend)
        state.append(np.mean(price_history[-3:]))  # 3-day average
        state.append(np.std(price_history[-7:]))   # Volatility

        # Pad to state_dim if necessary
        while len(state) < self.state_dim:
            state.append(0.0)

        return np.array(state[:self.state_dim], dtype=np.float32)

    def select_action(self, state, deterministic=False):
        """
        Select bid action using epsilon-greedy strategy.
        """
        if not deterministic and random.random() < self.epsilon:
            # Explore: random action
            action_idx = random.randrange(self.action_dim)
        else:
            # Exploit: best action from Q-network
            with torch.no_grad():
                state_tensor = torch.FloatTensor(state).unsqueeze(0)
                q_values = self.policy_net(state_tensor)
                action_idx = q_values.argmax().item()

        # Decode action index to volume and price
        volume_idx = action_idx // self.price_actions
        price_idx = action_idx % self.price_actions

        volume_mw = self.volume_levels[volume_idx]
        price_inr_per_kwh = self.price_levels[price_idx]

        return {
            'action_idx': action_idx,
            'volume_mw': volume_mw,
            'price_inr_kwh': price_inr_per_kwh
        }

    def store_transition(self, state, action_idx, reward, next_state, done):
        """
        Store experience in replay memory.
        """
        self.memory.append((state, action_idx, reward, next_state, done))

    def train_step(self):
        """
        Perform one training step using experience replay.
        """
        if len(self.memory) < self.batch_size:
            return None

        # Sample batch
        batch = random.sample(self.memory, self.batch_size)
        states, actions, rewards, next_states, dones = zip(*batch)

        states = torch.FloatTensor(states)
        actions = torch.LongTensor(actions)
        rewards = torch.FloatTensor(rewards)
        next_states = torch.FloatTensor(next_states)
        dones = torch.FloatTensor(dones)

        # Current Q values
        current_q = self.policy_net(states).gather(1, actions.unsqueeze(1))

        # Next Q values from target network
        with torch.no_grad():
            next_q = self.target_net(next_states).max(1)[0]
            target_q = rewards + (1 - dones) * self.gamma * next_q

        # Compute loss
        loss = nn.MSELoss()(current_q.squeeze(), target_q)

        # Optimize
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.policy_net.parameters(), 1.0)
        self.optimizer.step()

        # Decay epsilon
        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay

        return loss.item()

    def update_target_network(self):
        """
        Update target network with policy network weights.
        """
        self.target_net.load_state_dict(self.policy_net.state_dict())

    def calculate_reward(self, bid_volume, bid_price, clearing_price,
                        actual_generation, scheduled_generation):
        """
        Calculate reward for the taken action.
        """
        # Check if bid was accepted (bid price <= clearing price)
        if bid_price <= clearing_price:
            accepted_volume = min(bid_volume, actual_generation)
            revenue = accepted_volume * clearing_price  # In MW × ₹/kWh
        else:
            accepted_volume = 0
            revenue = 0

        # Deviation penalty (DSM charges)
        deviation = abs(actual_generation - scheduled_generation)
        dsm_rate = 2.0  # ₹/kWh penalty rate
        deviation_penalty = deviation * dsm_rate

        # Opportunity cost (energy not sold)
        unsold_energy = max(0, actual_generation - accepted_volume)
        opportunity_cost = unsold_energy * clearing_price * 0.5  # Partial penalty

        # Net reward
        reward = revenue - deviation_penalty - opportunity_cost

        # Normalize reward to typical range
        normalized_reward = reward / 10000  # Scale down for training stability

        return normalized_reward

    def save_model(self, filepath):
        """Save trained model."""
        torch.save({
            'policy_net': self.policy_net.state_dict(),
            'target_net': self.target_net.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'epsilon': self.epsilon
        }, filepath)

    def load_model(self, filepath):
        """Load trained model."""
        checkpoint = torch.load(filepath)
        self.policy_net.load_state_dict(checkpoint['policy_net'])
        self.target_net.load_state_dict(checkpoint['target_net'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.epsilon = checkpoint['epsilon']