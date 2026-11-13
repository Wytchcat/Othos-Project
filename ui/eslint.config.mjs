import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';
import prettierRecommended from 'eslint-plugin-prettier/recommended';
import { defineConfig, globalIgnores } from 'eslint/config';

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  prettierRecommended,
  {
    // Vendored from Agents UI (overwritten by `pnpm shadcn:install`). The React Compiler
    // rules added in eslint-config-next 16 flag upstream patterns here, so report them
    // as warnings instead of patching code that a sync would replace.
    files: ['components/agents-ui/**', 'hooks/agents-ui/**'],
    rules: {
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/static-components': 'warn',
    },
  },
  // public/othos-board: third-party ai-visualizer code (AGPL), kept close to upstream
  globalIgnores(['.next/**', 'out/**', 'build/**', 'next-env.d.ts', 'public/othos-board/**']),
]);

export default eslintConfig;
