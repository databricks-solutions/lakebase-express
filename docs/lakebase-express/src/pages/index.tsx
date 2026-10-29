import type { JSX } from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';

const STEPS: { title: string; desc: string; post?: boolean }[] = [
  { title: 'Connections & Target', desc: 'Configure and test the source and the Lakebase target.' },
  { title: 'Assessment', desc: 'Scan the source, score compatibility, size Lakebase and estimate cost.' },
  { title: 'Schema & Code', desc: 'Review an editable plan: DDL plus AI-translated PL/pgSQL.' },
  { title: 'Data Migration', desc: 'Pick the tables and the load options.' },
  { title: 'Create Sync', desc: 'Run it in-app, or offload a PySpark snapshot to a Databricks Job.' },
  { title: 'Validate', desc: 'Validation, Query Parity, the app-migration skill and the audit report.', post: true },
];

const CARDS: { title: string; desc: string; to: string }[] = [
  { title: 'Installation', desc: 'Deploy the Databricks App: bundle target, secret scope, grants and network access.', to: '/docs/installation/' },
  { title: 'User guide', desc: 'The migration workflow, module by module — including in-app (sync) and job (async) loads.', to: '/docs/guide/' },
  { title: 'Architecture', desc: 'How the engine, connectors, persistence and REST API fit together.', to: '/docs/architecture/' },
  { title: 'Reference', desc: 'Configuration variables, collation mapping, limitations and troubleshooting.', to: '/docs/reference/' },
  { title: 'Development', desc: 'Project layout, local development, tests, CI and adding a source connector.', to: '/docs/dev/' },
];

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Migrate to Databricks Lakebase"
      description="Lakebase Express — a Databricks App that migrates a transactional database to Databricks Lakebase."
    >
      <header className="lbx-hero">
        <img src={useBaseUrl('/img/logo.svg')} alt="" className="lbx-hero__logo" />
        <h1 className="lbx-hero__title">Lakebase Express</h1>
        <p className="lbx-hero__tagline">
          A Databricks App that guides you through migrating a transactional database to
          Databricks Lakebase. A migration engine, not a code generator: you review each
          artifact, and the app applies it and streams the data in with live progress.
        </p>
        <p className="lbx-hero__provided">Sources today: Azure SQL and SQL Server</p>
        <div className="lbx-hero__actions">
          <Link className="button button--primary button--lg" to="/docs/installation/">
            Get started
          </Link>
          <Link className="button button--secondary button--lg" to="/docs/motivation/">
            Why Lakebase Express
          </Link>
        </div>
      </header>

      <main>
        <section className="lbx-section">
          <h2 className="lbx-section__title">One project, end to end</h2>
          <p className="lbx-section__lead">
            Each migration is a saved project you can leave and resume. Modules are
            independent — work through them in order, or jump back to any of them.
          </p>
          <div className="lbx-steps">
            {STEPS.map((s) => (
              <div key={s.title} className={s.post ? 'lbx-step lbx-step--post' : 'lbx-step'}>
                <h3>{s.title}</h3>
                <p>{s.desc}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="lbx-section">
          <img
            src={useBaseUrl('/img/migrations-list.png')}
            alt="The migrations list: each saved project with its source, Lakebase target, and per-stage progress."
            className="lbx-screenshot"
          />
        </section>

        <section className="lbx-section">
          <h2 className="lbx-section__title">Documentation</h2>
          <div className="lbx-cards">
            {CARDS.map((c) => (
              <Link key={c.title} to={c.to} className="lbx-card">
                <h3>{c.title}</h3>
                <p>{c.desc}</p>
              </Link>
            ))}
          </div>
        </section>

        <div className="lbx-disclaimer alert alert--warning" role="note">
          <strong>Not an official Databricks product.</strong> Lakebase Express is a solution
          accelerator provided as is, without warranty, support commitment or SLA. Review every
          generated artifact and try it on non-production data first.{' '}
          <Link to="/docs/motivation/#disclaimer">Read the full disclaimer</Link>.
        </div>
      </main>
    </Layout>
  );
}
