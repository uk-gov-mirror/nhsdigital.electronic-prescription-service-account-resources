# Generated new templates

Run the following to create new templates
```
export PATH=$PATH:$PWD/node_modules/.bin
export AWS_REGION="eu-west-2"
export CDK_APP_NAME="AccountResources"
export CDK_CONFIG_isPullRequest=false
export CDK_CONFIG_environment="dev"
export CDK_CONFIG_commitId="706718162ffce4d7bbbf1c9c635e96e5a336ebf2"
export CDK_CONFIG_versionNumber="v1.11.2"
export REQUIRE_APPROVAL="never"

eval "$(tsx environmentSettings/setCdkConfig.ts)"
npm run cdk-synth --workspace packages/cdk/

```

# Migrate regression test secrets into secrets stack
Use the following command to migrate secrets from account-resources into secrets stack

```
python 'packages/cdk/scripts/move_stack_resources.py' \
    --base_stack account-resources-cdk-uk \
    --destination_stack secrets-cdk \
    --migration_stage regression_test_secrets \
    --resources_file packages/cdk/migration/regression_test_secrets.json \
    --base_stack_template cdk.out/AccountResources_UK.template.json \
    --destination_stack_template cdk.out/Secrets.template.json
```
