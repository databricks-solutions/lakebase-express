import { themes as prismThemes } from 'prism-react-renderer';
import type { Config } from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// Runs in Node.js at build time — no browser APIs or JSX here.

const REPO = 'https://github.com/databricks-solutions/lakebase-express';

const config: Config = {
  title: 'Lakebase Express',
  tagline: 'Migrate a transactional database to Databricks Lakebase — assess, plan, apply, load, and validate from one Databricks App.',
  favicon: 'img/logo.svg',

  // Served from GitHub Pages at https://databricks-solutions.github.io/lakebase-express/
  url: 'https://databricks-solutions.github.io',
  baseUrl: '/lakebase-express/',
  trailingSlash: true,
  organizationName: 'databricks-solutions',
  projectName: 'lakebase-express',

  // A broken link fails the build, so a renamed page cannot ship a dead reference.
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  onDuplicateRoutes: 'throw',

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  plugins: ['docusaurus-plugin-image-zoom', 'docusaurus-lunr-search'],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: `${REPO}/tree/main/docs/lakebase-express/`,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Lakebase Express',
      logo: {
        alt: 'Lakebase Express logo',
        src: 'img/logo.svg',
      },
      items: [
        { type: 'docSidebar', sidebarId: 'docsSidebar', position: 'left', label: 'Docs' },
        { to: '/docs/guide/', label: 'User guide', position: 'left' },
        { to: '/docs/architecture/', label: 'Architecture', position: 'left' },
        { type: 'search', position: 'right' },
        {
          href: REPO,
          position: 'right',
          className: 'header-github-link',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            { label: 'Installation', to: '/docs/installation/' },
            { label: 'User guide', to: '/docs/guide/' },
            { label: 'Architecture', to: '/docs/architecture/' },
          ],
        },
        {
          title: 'Project',
          items: [
            { label: 'GitHub', href: REPO },
            { label: 'Issues', href: `${REPO}/issues` },
            { label: 'License', href: `${REPO}/blob/main/LICENSE.md` },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Databricks, Inc. Not an official Databricks product — provided as is, without warranty. Docs built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.oneLight,
      darkTheme: prismThemes.oneDark,
      additionalLanguages: ['bash', 'sql', 'yaml', 'json', 'python'],
    },
    zoom: {
      selector: 'article img',
      background: {
        light: '#ffffff',
        dark: '#1b1b1d',
      },
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
