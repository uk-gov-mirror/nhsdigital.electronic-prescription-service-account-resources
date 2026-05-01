#!/usr/bin/env python3
"""Rename CloudFormation logical resource IDs within a single stack.

This script fetches a deployed stack template, renames logical resource IDs as
specified in a mapping file, writes the renamed template locally, compares it
against an expected template, and (after confirmation) uploads the result to S3
and creates a CloudFormation stack refactor.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


ARTIFACTS_BUCKET_EXPORT_NAME = 'account-resources-cdk-uk:Bucket:ArtifactsBucket:Arn'
S3_URL_REGION = 'eu-west-2'


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Fetch a CloudFormation stack template, rename logical resource IDs as '
            'specified in a mapping file, compare the result against an expected template, '
            'then upload and create a stack refactor.'
        )
    )
    parser.add_argument('--base_stack', required=True, help='Stack name to rename resources within')
    parser.add_argument(
        '--migration_stage',
        required=True,
        help='Migration stage label used in the upload path and stack refactor description',
    )
    parser.add_argument(
        '--resource_mapping_file',
        required=True,
        help=(
            'Path to a JSON file containing an array of resource rename mappings, '
            'each with Source and Destination StackName and LogicalResourceId fields'
        ),
    )
    parser.add_argument(
        '--base_stack_template',
        required=True,
        help='Path to a JSON file to compare against the generated renamed template',
    )
    return parser.parse_args()


def _read_resource_mappings(mapping_file: Path, base_stack: str) -> list[dict[str, Any]]:
    try:
        with mapping_file.open('r', encoding='utf-8') as file_handle:
            parsed = json.load(file_handle)
    except FileNotFoundError as exc:
        raise ValueError(f'Resource mapping file not found: {mapping_file}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'Resource mapping file is not valid JSON: {mapping_file}') from exc

    if not isinstance(parsed, list):
        raise ValueError('Resource mapping file must be a JSON array')

    for index, entry in enumerate(parsed):
        for field in ('Source', 'Destination'):
            if not isinstance(entry.get(field), dict):
                raise ValueError(f'Entry {index} is missing a valid "{field}" object')
            for key in ('StackName', 'LogicalResourceId'):
                if not isinstance(entry[field].get(key), str) or not entry[field][key]:
                    raise ValueError(f'Entry {index} {field}.{key} must be a non-empty string')

        source_stack = entry['Source']['StackName']
        destination_stack = entry['Destination']['StackName']
        if source_stack != base_stack or destination_stack != base_stack:
            raise ValueError(
                f'Entry {index} StackName values must both equal the base stack "{base_stack}", '
                f'got Source="{source_stack}" Destination="{destination_stack}"'
            )

    source_ids = [entry['Source']['LogicalResourceId'] for entry in parsed]
    duplicates = [logical_id for logical_id in source_ids if source_ids.count(logical_id) > 1]
    if duplicates:
        raise ValueError(
            f'Resource mapping file contains duplicate source logical IDs: {", ".join(sorted(set(duplicates)))}'
        )

    destination_ids = [entry['Destination']['LogicalResourceId'] for entry in parsed]
    destination_duplicates = [
        logical_id for logical_id in destination_ids if destination_ids.count(logical_id) > 1
    ]
    if destination_duplicates:
        raise ValueError(
            f'Resource mapping file contains duplicate destination logical IDs: '
            f'{", ".join(sorted(set(destination_duplicates)))}'
        )

    return parsed


def _read_json_file(json_file: Path) -> dict[str, Any]:
    try:
        with json_file.open('r', encoding='utf-8') as file_handle:
            parsed = json.load(file_handle)
    except FileNotFoundError as exc:
        raise ValueError(f'JSON file not found: {json_file}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'JSON file is not valid: {json_file}') from exc

    if not isinstance(parsed, dict):
        raise ValueError(f'JSON file must contain a JSON object: {json_file}')

    return parsed


def _get_template_body(cloudformation: Any, stack_name: str) -> dict[str, Any]:
    try:
        response = cloudformation.get_template(StackName=stack_name)
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f'Failed to fetch template for stack: {stack_name}') from exc

    template_body = response.get('TemplateBody')
    if isinstance(template_body, str):
        try:
            return json.loads(template_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f'Template for stack {stack_name} is not valid JSON and cannot be processed by this script'
            ) from exc

    if isinstance(template_body, dict):
        return template_body

    raise RuntimeError(f'Unexpected template format returned for stack: {stack_name}')


def _write_json_file(path: Path, content: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as file_handle:
        json.dump(content, file_handle, indent=2)
        file_handle.write('\n')


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as file_handle:
        file_handle.write(content)


def _get_export_value(cloudformation: Any, export_name: str) -> str:
    next_token: str | None = None

    while True:
        request: dict[str, str] = {}
        if next_token:
            request['NextToken'] = next_token

        try:
            response = cloudformation.list_exports(**request)
        except (ClientError, BotoCoreError) as exc:
            raise RuntimeError(f'Failed to list CloudFormation exports while looking up {export_name}') from exc

        for export in response.get('Exports', []):
            if export.get('Name') == export_name:
                export_value = export.get('Value')
                if not isinstance(export_value, str) or not export_value:
                    raise RuntimeError(f'CloudFormation export {export_name} does not contain a valid value')
                return export_value

        next_token = response.get('NextToken')
        if not next_token:
            break

    raise ValueError(f'CloudFormation export not found: {export_name}')


def _bucket_name_from_arn(bucket_arn: str) -> str:
    prefix = 'arn:aws:s3:::'
    if not bucket_arn.startswith(prefix):
        raise ValueError(f'Artifacts bucket export is not a valid S3 bucket ARN: {bucket_arn}')

    bucket_name = bucket_arn[len(prefix):]
    if not bucket_name:
        raise ValueError(f'Artifacts bucket export is missing the bucket name: {bucket_arn}')

    return bucket_name


def _upload_file_to_s3(s3_client: Any, local_path: Path, bucket_name: str, object_key: str) -> str:
    try:
        s3_client.upload_file(str(local_path), bucket_name, object_key)
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f'Failed to upload {local_path} to s3://{bucket_name}/{object_key}') from exc

    return f'https://s3.{S3_URL_REGION}.amazonaws.com/{bucket_name}/{object_key}'


def _write_template_differences(
    diff_file: Path,
    comparison_name: str,
    expected_template: dict[str, Any],
    generated_template: dict[str, Any],
) -> None:
    expected_lines = json.dumps(expected_template, indent=2).splitlines()
    generated_lines = json.dumps(generated_template, indent=2).splitlines()
    differences = list(
        difflib.unified_diff(
            expected_lines,
            generated_lines,
            fromfile=f'{comparison_name} (provided)',
            tofile=f'{comparison_name} (generated)',
            lineterm='',
        )
    )

    if differences:
        _write_text_file(diff_file, '\n'.join(differences) + '\n')
    else:
        _write_text_file(diff_file, f'No differences found for {comparison_name}.\n')


def _confirm_continue(
    uploaded_url: str,
    stack_refactor_description: str,
    diff_file: Path,
) -> None:
    print('Prepared renamed template for upload:')
    print(f'  Template upload target: {uploaded_url}')
    print(f'  Stack refactor description: {stack_refactor_description}')
    print(f'  Template differences: {diff_file}')
    print('Review the renamed template and differences before confirming.')

    try:
        response = input('Continue with S3 upload and CloudFormation stack refactor? [y/N]: ')
    except EOFError as exc:
        raise RuntimeError('Confirmation required before upload, but no interactive input was available') from exc

    if response.strip().lower() not in {'y', 'yes'}:
        raise RuntimeError('Operation cancelled before S3 upload')


def _create_stack_refactor(
    cloudformation: Any,
    base_stack: str,
    template_url: str,
    resource_mappings: list[dict[str, Any]],
    description: str,
) -> str:
    try:
        response = cloudformation.create_stack_refactor(
            Description=description,
            EnableStackCreation=False,
            ResourceMappings=resource_mappings,
            StackDefinitions=[
                {
                    'StackName': base_stack,
                    'TemplateURL': template_url,
                },
            ],
        )
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError('Failed to create CloudFormation stack refactor') from exc

    stack_refactor_id = response.get('StackRefactorId')
    if not isinstance(stack_refactor_id, str) or not stack_refactor_id:
        raise RuntimeError('CloudFormation stack refactor response did not include StackRefactorId')

    return stack_refactor_id


def _rename_resources(
    template: dict[str, Any],
    resource_mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    resources = template.get('Resources')
    if not isinstance(resources, dict):
        raise ValueError('Template does not contain a valid Resources object')

    rename_map = {
        entry['Source']['LogicalResourceId']: entry['Destination']['LogicalResourceId']
        for entry in resource_mappings
    }

    missing = [old_id for old_id in rename_map if old_id not in resources]
    if missing:
        raise ValueError(f'Resources not found in stack template: {", ".join(missing)}')

    collisions = [
        new_id
        for old_id, new_id in rename_map.items()
        if new_id in resources and new_id not in rename_map
    ]
    if collisions:
        raise ValueError(
            f'Destination logical IDs already exist in template and are not themselves being renamed: '
            f'{", ".join(collisions)}'
        )

    renamed_resources = {
        rename_map.get(resource_id, resource_id): resource_value
        for resource_id, resource_value in resources.items()
    }

    return {
        **template,
        'Resources': renamed_resources,
    }


def main() -> int:
    try:
        args = _parse_args()

        base_stack = args.base_stack
        migration_stage = args.migration_stage
        mapping_file = Path(args.resource_mapping_file)
        base_stack_template_path = Path(args.base_stack_template)
        timestamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
        stack_refactor_description = f'{migration_stage} {timestamp}'

        resource_mappings = _read_resource_mappings(mapping_file, base_stack)

        cloudformation = boto3.client('cloudformation')
        s3_client = boto3.client('s3')

        base_template = _get_template_body(cloudformation, base_stack)

        output_dir = Path('cdk.out')
        downloaded_template_file = output_dir / f'{base_stack}.template.json'
        _write_json_file(downloaded_template_file, base_template)

        renamed_template = _rename_resources(base_template, resource_mappings)

        renamed_template_file = output_dir / f'{base_stack}.rename.json'
        _write_json_file(renamed_template_file, renamed_template)

        diff_file = output_dir / f'{base_stack}.rename.diff.txt'
        expected_template = _read_json_file(base_stack_template_path)
        _write_template_differences(diff_file, 'base stack template', expected_template, renamed_template)

        artifacts_bucket_arn = _get_export_value(cloudformation, ARTIFACTS_BUCKET_EXPORT_NAME)
        artifacts_bucket_name = _bucket_name_from_arn(artifacts_bucket_arn)

        upload_prefix = f'migration/{migration_stage}/{timestamp}'
        uploaded_key = f'{upload_prefix}/{renamed_template_file.name}'
        uploaded_url = f'https://s3.{S3_URL_REGION}.amazonaws.com/{artifacts_bucket_name}/{uploaded_key}'

        _confirm_continue(uploaded_url, stack_refactor_description, diff_file)

        uploaded_url = _upload_file_to_s3(s3_client, renamed_template_file, artifacts_bucket_name, uploaded_key)

        cf_resource_mappings = [
            {
                'Source': entry['Source'],
                'Destination': entry['Destination'],
            }
            for entry in resource_mappings
        ]

        stack_refactor_id = _create_stack_refactor(
            cloudformation,
            base_stack,
            uploaded_url,
            cf_resource_mappings,
            stack_refactor_description,
        )

        print(f'Wrote {downloaded_template_file}')
        print(f'Wrote {renamed_template_file}')
        print(f'Wrote {diff_file}')
        print(f'Uploaded {renamed_template_file} to {uploaded_url}')
        print(f'StackRefactorId: {stack_refactor_id}')
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
