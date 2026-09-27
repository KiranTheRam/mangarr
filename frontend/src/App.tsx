import { useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import CommandPalette from "./components/CommandPalette";
import Sidebar from "./components/Sidebar";
import Library from "./pages/Library";
import AddSeries from "./pages/AddSeries";
import Calendar from "./pages/Calendar";
import ImportLists from "./pages/ImportLists";
import LibraryImport from "./pages/LibraryImport";
import SeriesDetail from "./pages/SeriesDetail";
import Activity from "./pages/Activity";
import Wanted from "./pages/Wanted";
import Settings from "./pages/Settings";

export default function App() {
  const [paletteOpen, setPaletteOpen] = useState(false);
  return (
    <div className="app">
      <Sidebar onSearch={() => setPaletteOpen(true)} />
      <CommandPalette open={paletteOpen} setOpen={setPaletteOpen} />
      <div className="main">
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/add" element={<AddSeries />} />
          <Route path="/add/import" element={<LibraryImport />} />
          <Route path="/add/lists" element={<ImportLists />} />
          <Route path="/calendar" element={<Calendar />} />
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
