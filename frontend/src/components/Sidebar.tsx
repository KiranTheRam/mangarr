import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, appVersion } from "../api/client";
import type { QueueItem } from "../api/types";

const items = [
  { to: "/", label: "Library", icon: "▦" },
  { to: "/add", label: "Add New", icon: "+" },
  { to: "/activity", label: "Activity", icon: "⇅" },
  { to: "/wanted", label: "Wanted", icon: "!" },
  { to: "/settings", label: "Settings", icon: "⚙" },
];

export default function Sidebar() {
  const { data: queue } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 3000,
  });

  return (
    <div className="sidebar">
      <div className="sidebar-logo">
        <img className="logo-mark" src="/mangarr-icon.svg" alt="" />
        mangarr
      </div>
      <nav>
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
          >
            <span className="icon">{item.icon}</span>
            {item.label}
            {item.to === "/activity" && queue && queue.length > 0 && (
              <span className="nav-badge">{queue.length}</span>
            )}
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-footer">v{appVersion()}</div>
    </div>
  );
}
