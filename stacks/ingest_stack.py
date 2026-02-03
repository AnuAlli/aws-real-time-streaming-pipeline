"""
AWS CDK Stack for Real-Time Streaming Pipeline
Kinesis Data Streams → Lambda → S3

This module defines the complete infrastructure stack using AWS CDK.
"""

from aws_cdk import (
    Stack,
    Duration,
    aws_kinesis as kinesis,
    aws_lambda as _lambda,
    aws_s3 as s33,
    aws_lambda_event_sources as lambda_events,
    aws_iam as iam,
    aws_logs as logs,
    RemovalPolicy,
)
from constructs import Construct


class IngestStack(Stack):
    """
    Creates the real-time data streaming infrastructure.
    
    Resources Created:
    - Kinesis Data Stream (on-demand capacity)
    - S3 Bucket for raw data storage
    - Lambda function for processing
    - IAM roles and policies
    - CloudWatch Logs
    """
    
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        # S3 Bucket for raw data
        raw_bucket = s33.Bucket(
            self, "RawDataBucket",
            bucket_name=f"{self.account}-raw-data-{self.region}",
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
            versioned=True,
            encryption=s33.BucketEncryption.S3_MANAGED,
            block_public_access=s33.BlockPublicAccess.BLOCK_ALL,
        )
        
        # Kinesis Data Stream
        stream = kinesis.Stream(
            self, "KinesisStream",
            stream_name="real-time-stream",
            shard_count=1,  # Start with 1 shard, auto-scale based on throughput
            retention_period=Duration.hours(24),
            encryption=kinesis.StreamEncryption.MANAGED,
        )
        
        # Lambda execution role with least privilege
        lambda_role = iam.Role(
            self, "LambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        
        # Custom inline policy for Kinesis and S3
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kinesis:DescribeStreamSummary",
                    "kinesis:GetRecords",
                    "kinesis:GetShardIterator",
                    "kinesis:ListShards",
                    "kinesis:SubscribeToShard",
                ],
                resources=[stream.stream_arn],
            )
        )
        
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                resources=[
                    raw_bucket.bucket_arn,
                    f"{raw_bucket.bucket_arn}/*",
                ],
            )
        )
        
        # CloudWatch Logs policy
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],  # Log group names are dynamic
            )
        )
        
        # Processing Lambda function
        processor_fn = _lambda.Function(
            self, "KinesisProcessor",
            function_name="kinesis-stream-processor",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="kinesis_processor.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.minutes(5),
            memory_size=256,
            role=lambda_role,
            environment={
                "DATA_BUCKET_NAME": raw_bucket.bucket_name,
                "LOG_LEVEL": "INFO",
            },
            reserved_concurrent_executions=10,  # Prevent throttling
        )
        
        # Add Kinesis event source
        processor_fn.add_event_source(
            lambda_events.KinesisEventSource(
                stream,
                batch_size=100,  # Records per batch
                starting_position=_lambda.StartingPosition.TRIM_HORIZON,
                retry_attempts=3,
                max_record_age=Duration.hours(1),
                parallelization_factor=1,  # Process shards in parallel
            )
        )
        
        # Log group with retention
        log_group = logs.LogGroup(
            self, "ProcessorLogGroup",
            log_group_name=f"/aws/lambda/{processor_fn.function_name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        
        # Outputs
        self.raw_bucket = raw_bucket
        self.kinesis_stream = stream
        self.processor_function = processor_fn
        
        # Add stack outputs
        self._add_outputs()
    
    def _add_outputs(self):
        """Add CloudFormation outputs for easy reference"""
        from aws_cdk import CfnOutput
        
        CfnOutput(
            self, "KinesisStreamArn",
            value=self.kinesis_stream.stream_arn,
            description="Kinesis Data Stream ARN",
            export_name="KinesisStreamArn"
        )
        
        CfnOutput(
            self, "RawBucketName",
            value=self.raw_bucket.bucket_name,
            description="S3 Bucket for raw data",
            export_name="RawBucketName"
        )
        
        CfnOutput(
            self, "ProcessorFunctionName",
            value=self.processor_function.function_name,
            description="Lambda function name",
            export_name="ProcessorFunctionName"
        )
