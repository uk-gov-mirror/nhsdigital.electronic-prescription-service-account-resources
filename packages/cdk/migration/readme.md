# Migrate regression test secrets into secrets stack
Use the following command to migrate secrets from account-resources into secrets stack

```
 python 'packages/cdk/scripts/move_stack_resources.py' \
    --base_stack account-resources-cdk-uk \
    --destination_stack secrets-cdk \
    --migration_stage regression_test_secrets \
    --resources_file packages/cdk/migration/regression_test_secrets.json
```
