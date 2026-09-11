-- FinSQL Snowflake setup: least-privilege, read-only access for the agent.
-- Run as SECURITYADMIN / SYSADMIN. This role is the real guarantee against
-- destructive commands: the agent's user physically cannot write, whatever
-- SQL it produces.

-- 1. Dedicated compute with a hard cost ceiling
CREATE WAREHOUSE IF NOT EXISTS AI_WH
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;

CREATE RESOURCE MONITOR IF NOT EXISTS AI_WH_MONITOR
  WITH CREDIT_QUOTA = 10 FREQUENCY = MONTHLY START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 80 PERCENT DO NOTIFY
           ON 100 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE AI_WH SET RESOURCE_MONITOR = AI_WH_MONITOR;

-- Kill runaway queries (cartesian joins etc.) even if the app-side timeout fails
ALTER WAREHOUSE AI_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 60;

-- 2. Curated schema: the agent sees reporting views, never raw tables.
--    PII columns are dropped or masked in these views.
CREATE SCHEMA IF NOT EXISTS FINANCE.REPORTING;
-- e.g. CREATE SECURE VIEW FINANCE.REPORTING.FCT_INVOICES AS SELECT ... FROM FINANCE.RAW.INVOICES;

-- 3. Read-only role
CREATE ROLE IF NOT EXISTS AI_READER;
GRANT USAGE ON WAREHOUSE AI_WH TO ROLE AI_READER;
GRANT USAGE ON DATABASE FINANCE TO ROLE AI_READER;
GRANT USAGE ON SCHEMA FINANCE.REPORTING TO ROLE AI_READER;
GRANT SELECT ON ALL VIEWS IN SCHEMA FINANCE.REPORTING TO ROLE AI_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA FINANCE.REPORTING TO ROLE AI_READER;
GRANT SELECT ON ALL TABLES IN SCHEMA FINANCE.REPORTING TO ROLE AI_READER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA FINANCE.REPORTING TO ROLE AI_READER;
-- Deliberately NOT granted: INSERT/UPDATE/DELETE/TRUNCATE, CREATE *, OWNERSHIP,
-- USAGE on any other schema, IMPORTED PRIVILEGES on the SNOWFLAKE database
-- (account usage / query history), or any stage (no COPY INTO / GET / PUT).

-- 4. Service user: key-pair auth only, no password to leak
CREATE USER IF NOT EXISTS FINSQL_SVC
  DEFAULT_ROLE = AI_READER DEFAULT_WAREHOUSE = AI_WH
  DEFAULT_NAMESPACE = FINANCE.REPORTING
  TYPE = SERVICE
  COMMENT = 'FinSQL agent (read-only)';
-- Generate locally:  openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out finsql_rsa_key.p8 -nocrypt
--                    openssl rsa -in finsql_rsa_key.p8 -pubout -out finsql_rsa_key.pub
ALTER USER FINSQL_SVC SET RSA_PUBLIC_KEY = '<contents of finsql_rsa_key.pub without header/footer>';
GRANT ROLE AI_READER TO USER FINSQL_SVC;

-- 5. Optional: restrict where the service user can connect from
-- CREATE NETWORK POLICY FINSQL_POLICY ALLOWED_IP_LIST = ('x.x.x.x/32');
-- ALTER USER FINSQL_SVC SET NETWORK_POLICY = FINSQL_POLICY;

-- Audit: every agent query carries QUERY_TAG = 'finsql:<slack user id>'
-- SELECT query_text, query_tag, start_time FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
-- WHERE query_tag LIKE 'finsql:%' ORDER BY start_time DESC;
