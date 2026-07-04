import { Navigate, Route, Routes } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Library from "./pages/Library";
import AddSeries from "./pages/AddSeries";
import SeriesDetail from "./pages/SeriesDetail";
import Activity from "./pages/Activity";
import Wanted from "./pages/Wanted";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <div className="app">
      <Sidebar />
      <div className="main">
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/add" element={<AddSeries />} />
          <Route path="/series/:id" element={<SeriesDetail />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/wanted" element={<Wanted />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  );
}
