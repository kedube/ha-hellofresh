// ESLint for the HelloFresh card (custom_components/hellofresh/www), its browser tests and the
// CI helper scripts. The card has no build step, so this is the only static check beyond
// "does every module parse" (.github/scripts/check_card_syntax.py).
//
// ESLint itself is a dev dependency of tests/browser, the repository's one Node package:
//
//   (cd tests/browser && npm ci)
//   tests/browser/node_modules/.bin/eslint .
//
// The config imports nothing, so it resolves from the repository root without a root
// node_modules. The rules are ESLint's recommended set for catching real mistakes (an
// undefined name, an unused variable, an unreachable branch), not style: formatting is
// left to the code as written.

const browserGlobals = Object.fromEntries(
  [
    "window", "document", "navigator", "location", "history", "console", "customElements",
    "HTMLElement", "Element", "Node", "Event", "CustomEvent", "KeyboardEvent", "MouseEvent",
    "FocusEvent", "InputEvent", "MutationObserver", "ResizeObserver", "IntersectionObserver",
    "requestAnimationFrame", "cancelAnimationFrame", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "queueMicrotask", "structuredClone", "fetch", "URL",
    "URLSearchParams", "Intl", "CSS", "getComputedStyle", "matchMedia", "localStorage",
    "sessionStorage", "performance", "AbortController", "DOMParser", "Image", "Blob",
    "TextEncoder", "TextDecoder", "crypto", "screen", "devicePixelRatio", "innerWidth",
    "innerHeight", "scrollX", "scrollY", "self", "globalThis", "CSSStyleSheet", "ShadowRoot",
    "DocumentFragment", "HTMLInputElement", "HTMLSelectElement", "HTMLTextAreaElement",
    "HTMLButtonElement", "HTMLImageElement", "Response", "atob", "btoa",
  ].map((name) => [name, "readonly"]),
);

const nodeGlobals = Object.fromEntries(
  [
    "process", "console", "Buffer", "URL", "URLSearchParams", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "setImmediate", "queueMicrotask", "structuredClone",
    "globalThis", "AbortController", "TextEncoder", "TextDecoder", "performance", "fetch",
  ].map((name) => [name, "readonly"]),
);

// ESLint's "recommended" rules (eslint:recommended), listed here so the config needs no
// import of @eslint/js.
const recommended = {
  "constructor-super": "error",
  "for-direction": "error",
  "getter-return": "error",
  "no-async-promise-executor": "error",
  "no-case-declarations": "error",
  "no-class-assign": "error",
  "no-compare-neg-zero": "error",
  "no-cond-assign": "error",
  "no-const-assign": "error",
  "no-constant-binary-expression": "error",
  "no-constant-condition": ["error", { checkLoops: false }],
  "no-control-regex": "error",
  "no-debugger": "error",
  "no-delete-var": "error",
  "no-dupe-args": "error",
  "no-dupe-class-members": "error",
  "no-dupe-else-if": "error",
  "no-dupe-keys": "error",
  "no-duplicate-case": "error",
  "no-empty": ["error", { allowEmptyCatch: true }],
  "no-empty-character-class": "error",
  "no-empty-pattern": "error",
  "no-empty-static-block": "error",
  "no-ex-assign": "error",
  "no-extra-boolean-cast": "error",
  "no-fallthrough": "error",
  "no-func-assign": "error",
  "no-global-assign": "error",
  "no-import-assign": "error",
  "no-invalid-regexp": "error",
  "no-irregular-whitespace": "error",
  "no-loss-of-precision": "error",
  "no-misleading-character-class": "error",
  "no-new-native-nonconstructor": "error",
  "no-nonoctal-decimal-escape": "error",
  "no-obj-calls": "error",
  "no-octal": "error",
  "no-prototype-builtins": "error",
  "no-redeclare": "error",
  "no-regex-spaces": "error",
  "no-self-assign": "error",
  "no-setter-return": "error",
  "no-shadow-restricted-names": "error",
  "no-sparse-arrays": "error",
  "no-this-before-super": "error",
  "no-undef": "error",
  "no-unexpected-multiline": "error",
  "no-unreachable": "error",
  "no-unsafe-finally": "error",
  "no-unsafe-negation": "error",
  "no-unsafe-optional-chaining": "error",
  "no-unused-labels": "error",
  "no-unused-private-class-members": "error",
  "no-unused-vars": ["error", { args: "none", caughtErrors: "none", varsIgnorePattern: "^_" }],
  "no-useless-backreference": "error",
  "no-useless-catch": "error",
  "no-useless-escape": "error",
  "no-with": "error",
  "require-yield": "error",
  "use-isnan": "error",
  "valid-typeof": "error",
};

export default [
  {
    ignores: ["**/node_modules/**", "tests/browser/shots/**", ".venv/**"],
  },
  {
    // The card: ES modules loaded by the browser. Console output reaches every user's
    // browser console, so each deliberate call carries its own disable comment.
    files: ["custom_components/hellofresh/www/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: browserGlobals,
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: { ...recommended, "no-console": "error" },
  },
  {
    // Node scripts: the browser-test runner and the CI helpers.
    files: ["tests/browser/**/*.{js,mjs}", ".github/scripts/**/*.mjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...nodeGlobals, ...browserGlobals },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: recommended,
  },
];
