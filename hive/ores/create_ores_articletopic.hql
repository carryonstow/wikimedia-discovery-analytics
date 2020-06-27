CREATE TABLE `discovery.ores_articletopic` (
  `wikiid` string COMMENT 'MediaWiki database name',
  `page_id` int COMMENT 'MediaWiki page id',
  `page_namespace` int COMMENT 'MediaWiki namespace page_id belongs to',
  `articletopic` array<string> COMMENT 'ores articletopic predictions formatted as name|int_score for elasticsearch ingestion'
)
PARTITIONED BY (
  `year` int COMMENT 'Unpadded year topic collection starts at',
  `month` int COMMENT 'Unpadded month topic collection starts at',
  `day` int COMMENT 'Unpadded day topic collection starts at'
)
STORED AS PARQUET
LOCATION 'hdfs://analytics-hadoop/wmf/data/discovery/ores/articletopic_v2'
;
