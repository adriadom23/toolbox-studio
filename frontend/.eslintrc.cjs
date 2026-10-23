module.exports = {
  root: true,
  env: {
    browser: true,
    es2022: true,
    node: true,
  },
  extends: ["eslint:recommended", "plugin:vue/vue3-essential"],
  parserOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
  },
  rules: {
    "no-unused-vars": ["error", { argsIgnorePattern: "^_|^forceNative$", varsIgnorePattern: "^forceNative$" }],
    "vue/multi-word-component-names": "off",
  },
  overrides: [
    {
      files: ["tests/**/*.js"],
      env: { node: true },
      globals: {
        afterEach: "readonly",
        beforeEach: "readonly",
        describe: "readonly",
        expect: "readonly",
        it: "readonly",
        vi: "readonly",
      },
    },
  ],
};
