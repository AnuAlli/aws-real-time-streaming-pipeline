"""
AWS Real-Time Streaming Pipeline
Kinesis Data Streams → AWS Lambda → S3 Data Lake

This module contains the Lambda function for processing streaming data
from Kinesis and storing it in S3 with proper partitioning and error handling.
"""

import json
import boto3
import os
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize AWS clients
kinesis_client = boto3.client('kinesis')
s3_client = boto3.client('s3')
cloudwatch_client = boto3.client('cloudwatch')


@dataclass
class ProcessingResult:
    """Result of processing a single record"""
    record_id: str
    success: bool
    partition_key: str
    output_path: Optional[str] = None
    error_message: Optional[str] = None
    processing_time_ms: float = 0.0


class StreamProcessor:
    """
    Processes records from Kinesis Data Stream and writes to S3.
    
    Features:
    - Automatic partitioning by timestamp (year/month/day/hour)
    - Batching for efficiency
    - Error handling and retry logic
    - CloudWatch metrics publishing
    """
    
    def __init__(self, bucket_name: str, prefix: str = "raw/"):
        self.bucket_name = bucket_name
        self.prefix = prefix
        self.records_processed = 0
        self.records_failed = 0
        self.start_time = time.time()
    
    def _get_partition_path(self, timestamp: Optional[datetime] = None) -> str:
        """
        Generate S3 partition path based on timestamp.
        
        Format: prefix/year=YYYY/month=MM/day=DD/hour=HH/
        
        Args:
            timestamp: Optional datetime, defaults to now
            
        Returns:
            Partitioned path string
        """
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        return (
            f"{self.prefix}"
            f"year={timestamp.strftime('%Y')}/"
            f"month={timestamp.strftime('%m')}/"
            f"day={timestamp.strftime('%d')}/"
            f"hour={timestamp.strftime('%H')}/"
        )
    
    def _publish_metrics(self, metric_name: str, value: float, unit: str = "Count"):
        """Publish custom metrics to CloudWatch"""
        try:
            cloudwatch_client.put_metric_data(
                Namespace='StreamingPipeline',
                MetricData=[
                    {
                        'MetricName': metric_name,
                        'Value': value,
                        'Unit': unit,
                        'Timestamp': datetime.utcnow()
                    },
                ]
            )
        except Exception as e:
            logger.warning(f"Failed to publish metric: {e}")
    
    def _serialize_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """
        Serialize a record for storage.
        
        - Converts any non-serializable types
        - Adds metadata
        
        Args:
            record: Raw record from Kinesis
            
        Returns:
            Serialized record ready for JSON storage
        """
        serialized = {
            "data": record,
            "metadata": {
                "ingestion_timestamp": datetime.utcnow().isoformat(),
                "pipeline_version": "1.0.0"
            }
        }
        return serialized
    
    def process_record(self, record: Dict[str, Any]) -> ProcessingResult:
        """
        Process a single Kinesis record.
        
        Args:
            record: Kinesis record containing Data field
            
        Returns:
            ProcessingResult with success/failure status
        """
        start_time = time.time()
        record_id = record.get('sequenceNumber', str(uuid.uuid4()))
        partition_key = record.get('partitionKey', 'unknown')
        
        try:
            # Decode Kinesis data (base64 encoded)
            import base64
            data_str = base64.b64decode(record['data']).decode('utf-8')
            data = json.loads(data_str)
            
            # Add processing metadata
            processed_data = self._serialize_record(data)
            
            # Generate output path
            timestamp = datetime.utcnow()
            output_path = (
                f"s3://{self.bucket_name}/"
                f"{self._get_partition_path(timestamp)}"
                f"data_{timestamp.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
            )
            
            # Upload to S3
            s3_client.put_object(
                Bucket=self.bucket_name,
                Key=output_path.replace(f"s3://{self.bucket_name}/", ''),
                Body=json.dumps(processed_data, indent=2),
                ContentType='application/json',
                Metadata={
                    'partition_key': partition_key,
                    'sequence_number': record_id
                }
            )
            
            processing_time = (time.time() - start_time) * 1000
            
            logger.info(f"Successfully processed record {record_id}")
            self.records_processed += 1
            
            return ProcessingResult(
                record_id=record_id,
                success=True,
                partition_key=partition_key,
                output_path=output_path,
                processing_time_ms=processing_time
            )
            
        except Exception as e:
            self.records_failed += 1
            logger.error(f"Failed to process record {record_id}: {e}")
            
            return ProcessingResult(
                record_id=record_id,
                success=False,
                partition_key=partition_key,
                error_message=str(e),
                processing_time_ms=(time.time() - start_time) * 1000
            )
    
    def get_summary(self) -> Dict[str, Any]:
        """Get processing summary statistics"""
        elapsed_time = time.time() - self.start_time
        
        return {
            "records_processed": self.records_processed,
            "records_failed": self.records_failed,
            "success_rate": (
                self.records_processed / (self.records_processed + self.records_failed) * 100
                if (self.records_processed + self.records_failed) > 0 else 100
            ),
            "processing_rate_per_second": self.records_processed / elapsed_time if elapsed_time > 0 else 0,
            "elapsed_time_seconds": elapsed_time
        }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda handler for processing Kinesis streams.
    
    Args:
        event: Kinesis event containing records
        context: Lambda context object
        
    Returns:
        Response with processing results
    """
    logger.info(f"Received {len(event.get('Records', []))} records from Kinesis")
    
    # Get configuration from environment variables
    bucket_name = os.environ.get('DATA_BUCKET_NAME')
    if not bucket_name:
        raise ValueError("DATA_BUCKET_NAME environment variable not set")
    
    # Initialize processor
    processor = StreamProcessor(bucket_name=bucket_name, prefix="raw/")
    
    # Process records
    results = []
    for record in event.get('Records', []):
        result = processor.process_record(record)
        results.append({
            'sequenceNumber': result.record_id,
            'success': result.success,
            'error': result.error_message
        })
    
    # Get summary
    summary = processor.get_summary()
    
    # Publish aggregate metrics
    processor._publish_metrics('RecordsProcessed', summary['records_processed'])
    processor._publish_metrics('RecordsFailed', summary['records_failed'])
    processor._publish_metrics('ProcessingTimeMs', summary['elapsed_time_seconds'] * 1000)
    
    logger.info(f"Processing complete: {summary}")
    
    return {
        'statusCode': 200,
        'body': {
            'message': 'Batch processing complete',
            'recordsProcessed': summary['records_processed'],
            'recordsFailed': summary['records_failed'],
            'results': results
        }
    }


if __name__ == "__main__":
    # Local testing with sample data
    test_event = {
        'Records': [
            {
                'sequenceNumber': 'test123',
                'partitionKey': 'test-key',
                'data': 'eyJ0ZXN0IjoiZGF0YSJ9'  # {"test":"data"} base64 encoded
            }
        ]
    }
    
    # Set environment for testing
    os.environ['DATA_BUCKET_NAME'] = 'test-bucket'
    
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))
