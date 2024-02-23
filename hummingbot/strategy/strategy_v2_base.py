import asyncio
import importlib
import inspect
from collections import Counter
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Set

import pandas as pd
from pydantic import Field, validator

from hummingbot.client.config.config_data_types import BaseClientModel, ClientFieldData
from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import PositionMode
from hummingbot.core.event.events import ExecutorEvent
from hummingbot.core.pubsub import PubSub
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.smart_components.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.smart_components.executors.executor_orchestrator import ExecutorOrchestrator
from hummingbot.smart_components.models.base import SmartComponentStatus
from hummingbot.smart_components.models.executor_actions import (
    CreateExecutorAction,
    ExecutorAction,
    StopExecutorAction,
    StoreExecutorAction,
)
from hummingbot.smart_components.models.executors_info import ExecutorInfo
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class StrategyV2ConfigBase(BaseClientModel):
    """
    Base class for version 2 strategy configurations.
    """
    markets: Dict[str, Set[str]] = Field(
        default="binance_perpetual.JASMY-USDT,RLC-USDT",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: (
                "Enter markets in format 'exchange1.tp1,tp2:exchange2.tp1,tp2':"
            )
        )
    )
    candles_config: List[CandlesConfig] = Field(
        default="binance_perpetual.JASMY-USDT.1m.500:binance_perpetual.RLC-USDT.1m.500",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: (
                "Enter candle configs in format 'exchange1.tp1.interval1.max_records:"
                "exchange2.tp2.interval2.max_records':"
            )
        )
    )
    controllers_config: List[ControllerConfigBase] = Field(
        default_factory=list,
        client_data=ClientFieldData(
            prompt_on_new=False,
            prompt=lambda mi: "Enter controller configurations:"
        ))

    @validator('markets', pre=True)
    def parse_markets(cls, v) -> Dict[str, Set[str]]:
        if isinstance(v, str):
            return cls.parse_markets_str(v)
        elif isinstance(v, dict):
            return v
        raise ValueError("Invalid type for markets. Expected str or Dict[str, Set[str]]")

    @staticmethod
    def parse_markets_str(v: str) -> Dict[str, Set[str]]:
        markets_dict = {}
        if v.strip():
            exchanges = v.split(':')
            for exchange in exchanges:
                parts = exchange.split('.')
                if len(parts) != 2 or not parts[1]:
                    raise ValueError(f"Invalid market format in segment '{exchange}'. "
                                     "Expected format: 'exchange.tp1,tp2'")
                exchange_name, trading_pairs = parts
                markets_dict[exchange_name] = set(trading_pairs.split(','))
        return markets_dict

    @validator('candles_config', pre=True)
    def parse_candles_config(cls, v) -> List[CandlesConfig]:
        if isinstance(v, str):
            return cls.parse_candles_config_str(v)
        elif isinstance(v, list):
            return v
        raise ValueError("Invalid type for candles_config. Expected str or List[CandlesConfig]")

    @staticmethod
    def parse_candles_config_str(v: str) -> List[CandlesConfig]:
        configs = []
        if v.strip():
            entries = v.split(':')
            for entry in entries:
                parts = entry.split('.')
                if len(parts) != 4:
                    raise ValueError(f"Invalid candles config format in segment '{entry}'. "
                                     "Expected format: 'exchange.tradingpair.interval.maxrecords'")
                connector, trading_pair, interval, max_records_str = parts
                try:
                    max_records = int(max_records_str)
                except ValueError:
                    raise ValueError(f"Invalid max_records value '{max_records_str}' in segment '{entry}'. "
                                     "max_records should be an integer.")
                config = CandlesConfig(
                    connector=connector,
                    trading_pair=trading_pair,
                    interval=interval,
                    max_records=max_records
                )
                configs.append(config)
        return configs


class StrategyV2Base(ScriptStrategyBase):
    """
    V2StrategyBase is a base class for strategies that use the new smart components architecture.
    """
    pubsub: PubSub = PubSub()
    markets: Dict[str, Set[str]]

    @classmethod
    def init_markets(cls, config: StrategyV2ConfigBase):
        """
        Initialize the markets that the strategy is going to use. This method is called when the strategy is created in
        the start command. Can be overridden to implement custom behavior.
        """
        cls.markets = config.markets

    def __init__(self, connectors: Dict[str, ConnectorBase], config: Optional[StrategyV2ConfigBase] = None):
        super().__init__(connectors, config)
        # Initialize the executor orchestrator
        self.executor_orchestrator = ExecutorOrchestrator(strategy=self)

        self.executors_info: Dict[str, List[ExecutorInfo]] = {}

        # Create a queue to listen to actions from the controllers
        self.actions_queue = asyncio.Queue()
        self.listen_to_executor_actions_task: asyncio.Task = asyncio.create_task(self.listen_to_executor_actions())

        # Initialize the market data provider
        self.market_data_provider = MarketDataProvider(connectors)
        self.market_data_provider.initialize_candles_feed_list(config.candles_config)
        self.controllers: Dict[str, ControllerBase] = {}
        self.initialize_controllers(config.controllers_config)

    def initialize_controllers(self, controllers_config: List[ControllerConfigBase]):
        """
        Initialize the controllers based on the provided configuration.
        """
        for controller_config in controllers_config:
            self.add_controller(controller_config)

    def add_controller(self, config: ControllerConfigBase):
        module = importlib.import_module(config.__module__)
        for name, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, ControllerBase) and obj is not ControllerBase:
                controller = obj(config, self.market_data_provider, self.actions_queue)
                self.controllers[name] = controller
                self.pubsub.add_listener(ExecutorEvent.EXECUTOR_INFO_UPDATE, controller.handle_executor_update)
                break

    async def listen_to_executor_actions(self):
        """
        Asynchronously listen to actions from the controllers and execute them.
        """
        while True:
            try:
                action = await self.actions_queue.get()
                self.executor_orchestrator.execute_actions(action)
                self.update_executors_info()
            except Exception as e:
                self.logger().error(f"Error executing action: {e}", exc_info=True)

    def update_executors_info(self):
        """
        Update the local state of the executors and publish the updates to the active controllers.
        """
        try:
            self.executors_info = self.executor_orchestrator.get_executors_report()
            self.pubsub.trigger_event(ExecutorEvent.EXECUTOR_INFO_UPDATE, self.executors_info)
        except Exception as e:
            self.logger().error(f"Error updating executors info: {e}", exc_info=True)

    @staticmethod
    def is_perpetual(connector: str) -> bool:
        return "perpetual" in connector

    def on_stop(self):
        self.executor_orchestrator.stop()
        self.market_data_provider.stop()

    def on_tick(self):
        self.update_executors_info()
        if self.market_data_provider.ready:
            executor_actions: List[ExecutorAction] = self.determine_executor_actions()
            for action in executor_actions:
                self.executor_orchestrator.execute_action(action)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Determine actions based on the provided executor handler report.
        """
        actions = []
        actions.extend(self.create_actions_proposal())
        actions.extend(self.stop_actions_proposal())
        actions.extend(self.store_actions_proposal())
        return actions

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        """
        Create actions proposal based on the current state of the executors.
        """
        raise NotImplementedError

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        """
        Create a list of actions to stop the executors based on order refresh and early stop conditions.
        """
        raise NotImplementedError

    def store_actions_proposal(self) -> List[StoreExecutorAction]:
        """
        Create a list of actions to store the executors that have been stopped.
        """
        raise NotImplementedError

    def get_executors_by_controller(self, controller_id: str) -> List[ExecutorInfo]:
        return self.executors_info.get(controller_id, [])

    def get_all_executors(self) -> List[ExecutorInfo]:
        return [executor for executors in self.executors_info.values() for executor in executors]

    def set_leverage(self, connector: str, trading_pair: str, leverage: int):
        self.connectors[connector].set_leverage(trading_pair, leverage)

    def set_position_mode(self, connector: str, position_mode: PositionMode):
        self.connectors[connector].set_position_mode(position_mode)

    @staticmethod
    def filter_executors(executors: List[ExecutorInfo], filter_func: Callable[[ExecutorInfo], bool]) -> List[ExecutorInfo]:
        return [executor for executor in executors if filter_func(executor)]

    @staticmethod
    def executors_info_to_df(executors_info: List[ExecutorInfo]) -> pd.DataFrame:
        """
        Convert a list of executor handler info to a dataframe.
        """
        df = pd.DataFrame([ei.dict() for ei in executors_info])
        # Convert the enum values to integers
        df['status'] = df['status'].apply(lambda x: x.value)

        # Sort the DataFrame
        df.sort_values(by='status', ascending=True, inplace=True)

        # Convert back to enums for display
        df['status'] = df['status'].apply(SmartComponentStatus)
        return df[["id", "timestamp", "type", "status", "net_pnl_pct", "net_pnl_quote", "cum_fees_quote", "is_trading",
                   "filled_amount_quote", "close_type"]]

    def format_status(self) -> str:
        original_info = super().format_status()
        columns_to_show = ["id", "type", "status", "net_pnl_pct", "net_pnl_quote", "cum_fees_quote",
                           "filled_amount_quote", "is_trading", "close_type"]
        extra_info = []

        # Initialize global performance metrics
        global_realized_pnl_quote = Decimal(0)
        global_unrealized_pnl_quote = Decimal(0)
        global_volume_traded = Decimal(0)
        global_close_type_counts = Counter()

        # Process each controller
        for controller_id, executors_list in self.executors_info.items():
            extra_info.append(f"Controller: {controller_id}")

            # In memory executors info
            executors_df = self.executors_info_to_df(executors_list)
            extra_info.extend([format_df_for_printout(executors_df[columns_to_show], table_format="psql")])

            # Generate performance report for each controller
            performance_report = self.executor_orchestrator.generate_performance_report(controller_id)

            # Append performance metrics
            controller_performance_info = [
                f"Controller {controller_id} Performance:",
                f"Realized PNL (Quote): {performance_report.realized_pnl_quote:.2f}",
                f"Unrealized PNL (Quote): {performance_report.unrealized_pnl_quote:.2f}",
                f"Realized PNL (%): {performance_report.realized_pnl_pct:.2f}%",
                f"Unrealized PNL (%): {performance_report.unrealized_pnl_pct:.2f}%",
                f"Global PNL (Quote): {performance_report.global_pnl_quote:.2f}",
                f"Global PNL (%): {performance_report.global_pnl_pct:.2f}%",
                f"Total Volume Traded: {performance_report.volume_traded:.2f}"
            ]

            # Append close type counts
            if performance_report.close_type_counts:
                controller_performance_info.append("Close Types Count:")
                for close_type, count in performance_report.close_type_counts.items():
                    controller_performance_info.append(f"  {close_type}: {count}")

            if controller_id != "main":
                extra_info.extend(controller_performance_info)

            # Aggregate global metrics and close type counts
            global_realized_pnl_quote += performance_report.realized_pnl_quote
            global_unrealized_pnl_quote += performance_report.unrealized_pnl_quote
            global_volume_traded += performance_report.volume_traded
            global_close_type_counts.update(performance_report.close_type_counts)

        # Calculate and append global performance metrics
        global_pnl_quote = global_realized_pnl_quote + global_unrealized_pnl_quote
        global_pnl_pct = (global_pnl_quote / global_volume_traded) * 100 if global_volume_traded != 0 else Decimal(0)

        global_performance_summary = [
            "\nGlobal Performance Summary:",
            f"Global Realized PNL (Quote): {global_realized_pnl_quote:.2f}",
            f"Global Unrealized PNL (Quote): {global_unrealized_pnl_quote:.2f}",
            f"Global PNL (Quote): {global_pnl_quote:.2f}",
            f"Global PNL (%): {global_pnl_pct:.2f}%",
            f"Total Volume Traded (Global): {global_volume_traded:.2f}"
        ]

        # Append global close type counts
        if global_close_type_counts:
            global_performance_summary.append("Global Close Types Count:")
            for close_type, count in global_close_type_counts.items():
                global_performance_summary.append(f"  {close_type}: {count}")

        extra_info.extend(global_performance_summary)

        # Combine original and extra information
        format_status = f"{original_info}\n\n" + "\n".join(extra_info)
        return format_status
