import { NavLink, Outlet } from "react-router-dom";

const sections = [
  { to: "/explore", label: "Explore", end: true },
  { to: "/dashboard", label: "Dashboard", end: true },
  { to: "/markets", label: "Markets" },
  { to: "/wealth", label: "Wealth" },
  { to: "/scenarios", label: "Scenarios" },
  { to: "/backtest", label: "Backtest" },
  { to: "/rebalancing", label: "Rebalancing" },
];

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <h1>mehl</h1>
        <nav>
          {sections.map((s) => (
            <NavLink
              key={s.to}
              to={s.to}
              end={s.end}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {s.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
