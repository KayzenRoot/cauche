CREATE ROLE cauche_runtime NOLOGIN NOINHERIT;
CREATE SCHEMA cauche;
REVOKE ALL ON SCHEMA cauche FROM PUBLIC, anon, authenticated, service_role;
GRANT CONNECT ON DATABASE postgres TO cauche_runtime;
GRANT USAGE, CREATE ON SCHEMA cauche TO cauche_runtime;
ALTER ROLE cauche_runtime IN DATABASE postgres SET search_path TO cauche;
