/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Cognito hosted-UI base URL, e.g. https://<prefix>.auth.<region>.amazoncognito.com */
  readonly VITE_COGNITO_DOMAIN?: string;
  /** Public SPA app client id (no secret). */
  readonly VITE_COGNITO_CLIENT_ID?: string;
  /** OAuth redirect URI (the SPA origin), e.g. https://<dist>.cloudfront.net/ */
  readonly VITE_COGNITO_REDIRECT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
