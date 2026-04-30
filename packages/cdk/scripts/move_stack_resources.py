#!/usr/bin/env python3
"""Move selected CloudFormation resources from one stack template to another.

This script fetches templates for two deployed stacks, writes those templates to
`cdk.out`, moves selected logical resources from the base template to the
destination template, and then writes refactored templates back to `cdk.out`.
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
            'Fetch two CloudFormation templates, move selected logical resources '
            'from base stack to destination stack, upload the refactored templates, '
            'and create a stack refactor.'
        )
    )
    parser.add_argument('--base_stack', help='Source stack name to move resources from')
    parser.add_argument('--destination_stack', help='Destination stack name to move resources to')
    parser.add_argument('--migration_stage', help='Migration stage label used in upload path and refactor description')
    parser.add_argument(
        '--resources_file',
        help='Path to JSON file containing an array of logical resource IDs to move',
    )
    parser.add_argument(
        '--base_stack_template',
        help='Path to a JSON file to compare against the generated base refactored template',
    )
    parser.add_argument(
        '--destination_stack_template',
        help='Path to a JSON file to compare against the generated destination refactored template',
    )
    return parser.parse_args()


def _read_resources_to_move(resources_file: Path) -> list[str]:
    try:
        with resources_file.open('r', encoding='utf-8') as file_handle:
            parsed = json.load(file_handle)
    except FileNotFoundError as exc:
        raise ValueError(f'Resources file not found: {resources_file}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'Resources file is not valid JSON: {resources_file}') from exc

    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError('Resources file must be a JSON array of strings')

    duplicates = [resource_id for resource_id in parsed if parsed.count(resource_id) > 1]
    if duplicates:
        duplicate_list = ', '.join(sorted(set(duplicates)))
        raise ValueError(f'Resources file contains duplicate logical IDs: {duplicate_list}')

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


def _confirm_continue(
    base_uploaded_url: str,
    destination_uploaded_url: str,
    stack_refactor_description: str,
    base_diff_file: Path,
    destination_diff_file: Path,
) -> None:
    print('Prepared refactored templates for upload:')
    print(f'  Base template upload target: {base_uploaded_url}')
    print(f'  Destination template upload target: {destination_uploaded_url}')
    print(f'  Stack refactor description: {stack_refactor_description}')
    print(f'  Base template differences (if expected template provided): {base_diff_file}')
    print(f'  Destination template differences (if expected template provided): {destination_diff_file}')
    print('Review the refactored templates and differences before confirming')

    try:
        response = input('Continue with S3 upload and CloudFormation stack refactor? [y/N]: ')
    except EOFError as exc:
        raise RuntimeError('Confirmation required before upload, but no interactive input was available') from exc

    if response.strip().lower() not in {'y', 'yes'}:
        raise RuntimeError('Operation cancelled before S3 upload')


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as file_handle:
        file_handle.write(content)


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


def _create_stack_refactor(
    cloudformation: Any,
    base_stack: str,
    destination_stack: str,
    base_template_url: str,
    destination_template_url: str,
    description: str,
) -> str:
    try:
        response = cloudformation.create_stack_refactor(
            Description=description,
            EnableStackCreation=False,
            ResourceMappings=[],
            StackDefinitions=[
                {
                    'StackName': base_stack,
                    'TemplateURL': base_template_url,
                },
                {
                    'StackName': destination_stack,
                    'TemplateURL': destination_template_url,
                },
            ],
        )
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError('Failed to create CloudFormation stack refactor') from exc

    stack_refactor_id = response.get('StackRefactorId')
    if not isinstance(stack_refactor_id, str) or not stack_refactor_id:
        raise RuntimeError('CloudFormation stack refactor response did not include StackRefactorId')

    return stack_refactor_id


def _move_resources(
    base_template: dict[str, Any],
    destination_template: dict[str, Any],
    resources_to_move: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    base_resources = base_template.get('Resources')
    destination_resources = destination_template.get('Resources')

    if not isinstance(base_resources, dict):
        raise ValueError('Base template does not contain a valid Resources object')
    if not isinstance(destination_resources, dict):
        raise ValueError('Destination template does not contain a valid Resources object')

    missing_in_base = [resource_id for resource_id in resources_to_move if resource_id not in base_resources]
    if missing_in_base:
        missing_list = ', '.join(missing_in_base)
        raise ValueError(f'Resources not found in base stack template: {missing_list}')

    collisions = [resource_id for resource_id in resources_to_move if resource_id in destination_resources]
    if collisions:
        collision_list = ', '.join(collisions)
        raise ValueError(
            f'Resources already exist in destination stack template and cannot be overwritten: {collision_list}'
        )

    # Preserve ordering from the base template, not just the requested list order.
    resources_to_move_set = set(resources_to_move)
    ordered_resources_to_move = [resource_id for resource_id in base_resources if resource_id in resources_to_move_set]

    moved_resource_definitions = {
        resource_id: base_resources[resource_id]
        for resource_id in ordered_resources_to_move
    }

    refactored_base_resources = {
        resource_id: resource_value
        for resource_id, resource_value in base_resources.items()
        if resource_id not in moved_resource_definitions
    }

    refactored_destination_resources = {
        **destination_resources,
        **moved_resource_definitions,
    }

    refactored_base_template = {
        **base_template,
        'Resources': refactored_base_resources,
    }
    refactored_destination_template = {
        **destination_template,
        'Resources': refactored_destination_resources,
    }

    return refactored_base_template, refactored_destination_template


def main() -> int:
    try:
        args = _parse_args()

        base_stack = args.base_stack
        destination_stack = args.destination_stack
        migration_stage = args.migration_stage
        resources_file = Path(args.resources_file)
        base_stack_template_path = Path(args.base_stack_template)
        destination_stack_template_path = Path(args.destination_stack_template)
        timestamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
        stack_refactor_description = f'{migration_stage} {timestamp}'

        if (base_stack_template_path is None) != (destination_stack_template_path is None):
            raise ValueError(
                'Both --base_stack_template and --destination_stack_template must be provided together'
            )

        resources_to_move = _read_resources_to_move(resources_file)

        cloudformation = boto3.client('cloudformation')
        s3_client = boto3.client('s3')

        base_template = _get_template_body(cloudformation, base_stack)
        destination_template = _get_template_body(cloudformation, destination_stack)

        output_dir = Path('cdk.out')
        base_template_file = output_dir / f'{base_stack}.template.json'
        destination_template_file = output_dir / f'{destination_stack}.template.json'

        _write_json_file(base_template_file, base_template)
        _write_json_file(destination_template_file, destination_template)

        refactored_base_template, refactored_destination_template = _move_resources(
            base_template,
            destination_template,
            resources_to_move,
        )

        base_refactor_file = output_dir / f'{base_stack}.refactor.json'
        destination_refactor_file = output_dir / f'{destination_stack}.refactor.json'
        base_diff_file = output_dir / f'{base_stack}.refactor.diff.txt'
        destination_diff_file = output_dir / f'{destination_stack}.refactor.diff.txt'

        _write_json_file(base_refactor_file, refactored_base_template)
        _write_json_file(destination_refactor_file, refactored_destination_template)

        if base_stack_template_path and destination_stack_template_path:
            expected_base_template = _read_json_file(base_stack_template_path)
            expected_destination_template = _read_json_file(destination_stack_template_path)
            _write_template_differences(
                base_diff_file,
                'base stack template',
                expected_base_template,
                refactored_base_template,
            )
            _write_template_differences(
                destination_diff_file,
                'destination stack template',
                expected_destination_template,
                refactored_destination_template,
            )

        artifacts_bucket_arn = _get_export_value(cloudformation, ARTIFACTS_BUCKET_EXPORT_NAME)
        artifacts_bucket_name = _bucket_name_from_arn(artifacts_bucket_arn)

        upload_prefix = f'migration/{migration_stage}/{timestamp}'
        base_uploaded_key = f'{upload_prefix}/{base_refactor_file.name}'
        destination_uploaded_key = f'{upload_prefix}/{destination_refactor_file.name}'

        base_uploaded_url = f'https://s3.{S3_URL_REGION}.amazonaws.com/{artifacts_bucket_name}/{base_uploaded_key}'
        destination_uploaded_url = (
            f'https://s3.{S3_URL_REGION}.amazonaws.com/{artifacts_bucket_name}/{destination_uploaded_key}'
        )

        _confirm_continue(
            base_uploaded_url,
            destination_uploaded_url,
            stack_refactor_description,
            base_diff_file,
            destination_diff_file,
        )

        base_uploaded_url = _upload_file_to_s3(
            s3_client,
            base_refactor_file,
            artifacts_bucket_name,
            base_uploaded_key,
        )
        destination_uploaded_url = _upload_file_to_s3(
            s3_client,
            destination_refactor_file,
            artifacts_bucket_name,
            destination_uploaded_key,
        )

        stack_refactor_id = _create_stack_refactor(
            cloudformation,
            base_stack,
            destination_stack,
            base_uploaded_url,
            destination_uploaded_url,
            stack_refactor_description,
        )

        print(f'Wrote {base_template_file}')
        print(f'Wrote {destination_template_file}')
        print(f'Wrote {base_refactor_file}')
        print(f'Wrote {destination_refactor_file}')
        if base_stack_template_path and destination_stack_template_path:
            print(f'Wrote {base_diff_file}')
            print(f'Wrote {destination_diff_file}')
        print(f'Uploaded {base_refactor_file} to {base_uploaded_url}')
        print(f'Uploaded {destination_refactor_file} to {destination_uploaded_url}')
        print(f'StackRefactorId: {stack_refactor_id}')
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
