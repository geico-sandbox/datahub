import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from datahub.ingestion.source.kafka_connect.common import (
    KAFKA,
    BaseConnector,
    ConnectorManifest,
    KafkaConnectLineage,
)

logger = logging.getLogger(__name__)


class RegexRouterTransform:
    """Helper class to handle RegexRouter transformations for topic/table names."""

    def __init__(self, config: Dict[str, str]) -> None:
        self.transforms = self._parse_transforms(config)

    def _parse_transforms(self, config: Dict[str, str]) -> List[Dict[str, str]]:
        """Parse transforms configuration from connector config."""
        transforms_list: List[Dict[str, str]] = []

        # Get the transforms parameter
        transforms_param: str = config.get("transforms", "")
        if not transforms_param:
            return transforms_list

        # Parse individual transforms
        transform_names: List[str] = [
            name.strip() for name in transforms_param.split(",")
        ]

        for transform_name in transform_names:
            if not transform_name:
                continue
            transform_config: Dict[str, str] = {}
            transform_prefix: str = f"transforms.{transform_name}."

            # Extract transform configuration
            for key, value in config.items():
                if key.startswith(transform_prefix):
                    config_key: str = key[len(transform_prefix) :]
                    transform_config[config_key] = value

            # Only process RegexRouter transforms
            if (
                transform_config.get("type")
                == "org.apache.kafka.connect.transforms.RegexRouter"
            ):
                transform_config["name"] = transform_name
                transforms_list.append(transform_config)

        return transforms_list

    def apply_transforms(self, topic_name: str) -> str:
        """Apply RegexRouter transforms to the topic name using Java regex."""
        result: str = topic_name

        for transform in self.transforms:
            regex_pattern: Optional[str] = transform.get("regex")
            replacement: str = transform.get("replacement", "")

            if regex_pattern:
                try:
                    # Use Java Pattern and Matcher for exact Kafka Connect compatibility
                    from java.util.regex import Pattern

                    pattern = Pattern.compile(regex_pattern)
                    matcher = pattern.matcher(result)

                    if matcher.find():
                        # Reset matcher to beginning for replaceFirst
                        matcher.reset()
                        result = matcher.replaceFirst(replacement)
                        logger.debug(
                            f"Applied transform {transform['name']}: {topic_name} -> {result}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Invalid regex pattern in transform {transform['name']}: {e}"
                    )

        return str(result)


@dataclass
class ConfluentS3SinkConnector(BaseConnector):
    @dataclass
    class S3SinkParser:
        target_platform: str
        bucket: str
        topics_dir: str
        topics: Iterable[str]
        regex_router: RegexRouterTransform

    def _get_parser(self, connector_manifest: ConnectorManifest) -> S3SinkParser:
        # https://docs.confluent.io/kafka-connectors/s3-sink/current/configuration_options.html#s3
        bucket: Optional[str] = connector_manifest.config.get("s3.bucket.name")
        if not bucket:
            raise ValueError(
                "Could not find 's3.bucket.name' in connector configuration"
            )

        # https://docs.confluent.io/kafka-connectors/s3-sink/current/configuration_options.html#storage
        topics_dir: str = connector_manifest.config.get("topics.dir", "topics")

        # Create RegexRouterTransform instance
        regex_router: RegexRouterTransform = RegexRouterTransform(
            connector_manifest.config
        )

        return self.S3SinkParser(
            target_platform="s3",
            bucket=bucket,
            topics_dir=topics_dir,
            topics=connector_manifest.topic_names,
            regex_router=regex_router,
        )

    def extract_flow_property_bag(self) -> Dict[str, str]:
        # Mask/Remove properties that may reveal credentials
        flow_property_bag: Dict[str, str] = {
            k: v
            for k, v in self.connector_manifest.config.items()
            if k
            not in [
                "aws.access.key.id",
                "aws.secret.access.key",
                "s3.sse.customer.key",
                "s3.proxy.password",
            ]
        }
        return flow_property_bag

    def extract_lineages(self) -> List[KafkaConnectLineage]:
        try:
            parser: ConfluentS3SinkConnector.S3SinkParser = self._get_parser(
                self.connector_manifest
            )

            lineages: List[KafkaConnectLineage] = list()
            for topic in parser.topics:
                # Apply RegexRouter transformations using the RegexRouterTransform class
                transformed_topic: str = parser.regex_router.apply_transforms(topic)
                target_dataset: str = (
                    f"{parser.bucket}/{parser.topics_dir}/{transformed_topic}"
                )

                lineages.append(
                    KafkaConnectLineage(
                        source_dataset=topic,
                        source_platform="kafka",
                        target_dataset=target_dataset,
                        target_platform=parser.target_platform,
                    )
                )
            return lineages
        except Exception as e:
            self.report.warning(
                "Error resolving lineage for connector",
                self.connector_manifest.name,
                exc=e,
            )

        return []


@dataclass
class SnowflakeSinkConnector(BaseConnector):
    @dataclass
    class SnowflakeParser:
        database_name: str
        schema_name: str
        topics_to_tables: Dict[str, str]
        regex_router: RegexRouterTransform

    def get_table_name_from_topic_name(self, topic_name: str) -> str:
        """
        This function converts the topic name to a valid Snowflake table name using some rules.
        Refer below link for more info
        https://docs.snowflake.com/en/user-guide/kafka-connector-overview#target-tables-for-kafka-topics
        """
        table_name: str = re.sub("[^a-zA-Z0-9_]", "_", topic_name)
        if re.match("^[^a-zA-Z_].*", table_name):
            table_name = "_" + table_name
        # Connector  may append original topic's hash code as suffix for conflict resolution
        # if generated table names for 2 topics are similar. This corner case is not handled here.
        # Note that Snowflake recommends to choose topic names that follow the rules for
        # Snowflake identifier names so this case is not recommended by snowflake.
        return table_name

    def get_parser(
        self,
        connector_manifest: ConnectorManifest,
    ) -> SnowflakeParser:
        database_name: str = connector_manifest.config["snowflake.database.name"]
        schema_name: str = connector_manifest.config["snowflake.schema.name"]

        # Create RegexRouterTransform instance
        regex_router: RegexRouterTransform = RegexRouterTransform(
            connector_manifest.config
        )

        # Fetch user provided topic to table map
        provided_topics_to_tables: Dict[str, str] = {}
        if connector_manifest.config.get("snowflake.topic2table.map"):
            for each in connector_manifest.config["snowflake.topic2table.map"].split(
                ","
            ):
                topic, table = each.split(":")
                provided_topics_to_tables[topic.strip()] = table.strip()

        topics_to_tables: Dict[str, str] = {}
        # Extract lineage for only those topics whose data ingestion started
        for topic in connector_manifest.topic_names:
            # Apply transforms first to get the transformed topic name
            transformed_topic: str = regex_router.apply_transforms(topic)

            if topic in provided_topics_to_tables:
                # If user provided which table to get mapped with this topic
                topics_to_tables[topic] = provided_topics_to_tables[topic]
            else:
                # Use the transformed topic name to generate table name
                topics_to_tables[topic] = self.get_table_name_from_topic_name(
                    transformed_topic
                )

        return self.SnowflakeParser(
            database_name=database_name,
            schema_name=schema_name,
            topics_to_tables=topics_to_tables,
            regex_router=regex_router,
        )

    def extract_flow_property_bag(self) -> Dict[str, str]:
        # For all snowflake sink connector properties, refer below link
        # https://docs.snowflake.com/en/user-guide/kafka-connector-install#configuring-the-kafka-connector
        # remove private keys, secrets from properties
        flow_property_bag: Dict[str, str] = {
            k: v
            for k, v in self.connector_manifest.config.items()
            if k
            not in [
                "snowflake.private.key",
                "snowflake.private.key.passphrase",
                "value.converter.basic.auth.user.info",
            ]
        }

        return flow_property_bag

    def extract_lineages(self) -> List[KafkaConnectLineage]:
        lineages: List[KafkaConnectLineage] = list()
        parser: SnowflakeSinkConnector.SnowflakeParser = self.get_parser(
            self.connector_manifest
        )

        for topic, table in parser.topics_to_tables.items():
            target_dataset: str = f"{parser.database_name}.{parser.schema_name}.{table}"
            lineages.append(
                KafkaConnectLineage(
                    source_dataset=topic,
                    source_platform=KAFKA,
                    target_dataset=target_dataset,
                    target_platform="snowflake",
                )
            )

        return lineages


@dataclass
class BigQuerySinkConnector(BaseConnector):
    @dataclass
    class BQParser:
        project: str
        target_platform: str
        sanitizeTopics: bool
        transforms: List[Dict[str, str]]
        regex_router: RegexRouterTransform
        topicsToTables: Optional[str] = None
        datasets: Optional[str] = None
        defaultDataset: Optional[str] = None
        version: str = "v1"

    def get_parser(
        self,
        connector_manifest: ConnectorManifest,
    ) -> BQParser:
        project: str = connector_manifest.config["project"]
        sanitizeTopics: str = connector_manifest.config.get("sanitizeTopics") or "false"

        # Parse ALL transforms (original BigQuery logic)
        transform_names: List[str] = (
            self.connector_manifest.config.get("transforms", "").split(",")
            if self.connector_manifest.config.get("transforms")
            else []
        )
        transforms: List[Dict[str, str]] = []
        for name in transform_names:
            transform: Dict[str, str] = {"name": name}
            transforms.append(transform)
            for key in self.connector_manifest.config:
                if key.startswith(f"transforms.{name}."):
                    transform[key.replace(f"transforms.{name}.", "")] = (
                        self.connector_manifest.config[key]
                    )

        # Create RegexRouterTransform instance for RegexRouter-specific handling
        regex_router: RegexRouterTransform = RegexRouterTransform(
            connector_manifest.config
        )

        if "defaultDataset" in connector_manifest.config:
            defaultDataset: str = connector_manifest.config["defaultDataset"]
            return self.BQParser(
                project=project,
                defaultDataset=defaultDataset,
                target_platform="bigquery",
                sanitizeTopics=sanitizeTopics.lower() == "true",
                version="v2",
                transforms=transforms,
                regex_router=regex_router,
            )
        else:
            # version 1.6.x and similar configs supported
            datasets: str = connector_manifest.config["datasets"]
            topicsToTables: Optional[str] = connector_manifest.config.get(
                "topicsToTables"
            )

            return self.BQParser(
                project=project,
                topicsToTables=topicsToTables,
                datasets=datasets,
                target_platform="bigquery",
                sanitizeTopics=sanitizeTopics.lower() == "true",
                transforms=transforms,
                regex_router=regex_router,
            )

    def get_list(self, property: str) -> Iterable[Tuple[str, str]]:
        entries: List[str] = property.split(",")
        for entry in entries:
            key, val = entry.rsplit("=")
            yield (key.strip(), val.strip())

    def get_dataset_for_topic_v1(self, topic: str, parser: BQParser) -> Optional[str]:
        topicregex_dataset_map: Dict[str, str] = dict(self.get_list(parser.datasets))  # type: ignore
        from java.util.regex import Pattern

        for pattern, dataset in topicregex_dataset_map.items():
            patternMatcher = Pattern.compile(pattern).matcher(topic)
            if patternMatcher.matches():
                return dataset
        return None

    def sanitize_table_name(self, table_name: str) -> str:
        table_name = re.sub("[^a-zA-Z0-9_]", "_", table_name)
        if re.match("^[^a-zA-Z_].*", table_name):
            table_name = "_" + table_name

        return table_name

    def get_dataset_table_for_topic(
        self, topic: str, parser: BQParser
    ) -> Optional[str]:
        if parser.version == "v2":
            dataset: Optional[str] = parser.defaultDataset
            parts: List[str] = topic.split(":")
            if len(parts) == 2:
                dataset = parts[0]
                table = parts[1]
            else:
                table = parts[0]
        else:
            dataset = self.get_dataset_for_topic_v1(topic, parser)
            if dataset is None:
                return None

            table = topic
            if parser.topicsToTables:
                topicregex_table_map: Dict[str, str] = dict(
                    self.get_list(parser.topicsToTables)  # type: ignore
                )
                from java.util.regex import Pattern

                for pattern, tbl in topicregex_table_map.items():
                    patternMatcher = Pattern.compile(pattern).matcher(topic)
                    if patternMatcher.matches():
                        table = tbl
                        break

        if parser.sanitizeTopics:
            table = self.sanitize_table_name(table)
        return f"{dataset}.{table}"

    def extract_flow_property_bag(self) -> Dict[str, str]:
        # Mask/Remove properties that may reveal credentials
        flow_property_bag: Dict[str, str] = {
            k: v
            for k, v in self.connector_manifest.config.items()
            if k not in ["keyfile"]
        }

        return flow_property_bag

    def extract_lineages(self) -> List[KafkaConnectLineage]:
        lineages: List[KafkaConnectLineage] = list()
        parser: BigQuerySinkConnector.BQParser = self.get_parser(
            self.connector_manifest
        )
        if not parser:
            return lineages
        target_platform: str = parser.target_platform
        project: str = parser.project

        for topic in self.connector_manifest.topic_names:
            # Apply RegexRouter transformations using the RegexRouterTransform class
            transformed_topic: str = parser.regex_router.apply_transforms(topic)

            # Use the transformed topic to determine dataset/table
            dataset_table: Optional[str] = self.get_dataset_table_for_topic(
                transformed_topic, parser
            )
            if dataset_table is None:
                self.report.warning(
                    "Could not find target dataset for topic, please check your connector configuration"
                    f"{self.connector_manifest.name} : {transformed_topic} ",
                )
                continue
            target_dataset: str = f"{project}.{dataset_table}"

            lineages.append(
                KafkaConnectLineage(
                    source_dataset=topic,  # Keep original topic as source
                    source_platform=KAFKA,
                    target_dataset=target_dataset,
                    target_platform=target_platform,
                )
            )
        return lineages


BIGQUERY_SINK_CONNECTOR_CLASS = "com.wepay.kafka.connect.bigquery.BigQuerySinkConnector"
S3_SINK_CONNECTOR_CLASS = "io.confluent.connect.s3.S3SinkConnector"
SNOWFLAKE_SINK_CONNECTOR_CLASS = "com.snowflake.kafka.connector.SnowflakeSinkConnector"
