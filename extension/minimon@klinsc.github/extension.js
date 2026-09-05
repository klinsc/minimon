// minimon - top-bar monitor rendering the snapshot minimon-daemon.py writes.
import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const STATUS = GLib.get_home_dir() + '/.cache/minimon/status.json';
const BAR_W = 232;

function fmtRate(b) {
    for (const [u, div] of [['G', 1 << 30], ['M', 1 << 20], ['K', 1 << 10]])
        if (b >= div)
            return `${(b / div).toFixed(1)}${u}`;
    return `${Math.round(b)}B`;
}

function fmtCount(n) {
    for (const [u, div] of [['B', 1e9], ['M', 1e6], ['K', 1e3]])
        if (n >= div)
            return `${(n / div).toFixed(1)}${u}`;
    return `${n}`;
}

class MeterRow {
    constructor(name, fillClass) {
        this.box = new St.BoxLayout({vertical: true, style: 'spacing: 3px;'});
        const head = new St.BoxLayout();
        this.name = new St.Label({text: name, style_class: 'minimon-name',
            x_expand: true, x_align: Clutter.ActorAlign.START});
        this.sub = new St.Label({style_class: 'minimon-sub'});
        this.val = new St.Label({style_class: 'minimon-big',
            style: 'padding-left: 8px;'});
        head.add_child(this.name);
        head.add_child(this.sub);
        head.add_child(this.val);
        const track = new St.Widget({style_class: 'minimon-track'});
        this.fill = new St.Widget({style_class: `minimon-fill ${fillClass}`});
        track.add_child(this.fill);
        this.box.add_child(head);
        this.box.add_child(track);
    }

    update(frac, value, sub, heat) {
        const w = Math.max(2, Math.round(BAR_W * Math.min(1, Math.max(0, frac))));
        this.fill.style = `width: ${w}px;`;
        this.val.text = value;
        this.sub.text = sub;
        let cls = 'minimon-sub';
        if (heat !== null && heat >= 85)
            cls += ' minimon-hot';
        else if (heat !== null && heat >= 70)
            cls += ' minimon-warm';
        this.sub.style_class = cls;
    }
}

const Indicator = GObject.registerClass(
class MinimonIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.0, 'minimon', false);
        this._ext = ext;
        this._label = new St.Label({
            text: 'minimon…',
            style_class: 'minimon-panel-label',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this.add_child(this._label);
        this._buildMenu();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._refresh();
        });
        this._refresh();
    }

    _buildMenu() {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._card = new St.BoxLayout({vertical: true, style_class: 'minimon-card'});
        item.add_child(this._card);
        this.menu.addMenuItem(item);

        this._host = new St.Label({style_class: 'minimon-host', text: '…'});
        this._card.add_child(this._host);

        this._cpu = new MeterRow('CPU', 'minimon-fill-cpu');
        this._gpu = new MeterRow('GPU', 'minimon-fill-gpu');
        this._ram = new MeterRow('RAM', 'minimon-fill-ram');
        for (const r of [this._cpu, this._gpu, this._ram])
            this._card.add_child(r.box);

        this._foot = [];
        const grid = new St.BoxLayout({vertical: true, style: 'spacing: 3px; padding-top: 2px;'});
        for (let i = 0; i < 3; i++) {
            const row = new St.BoxLayout();
            const l = new St.Label({style_class: 'minimon-foot', x_expand: true,
                x_align: Clutter.ActorAlign.START});
            const r = new St.Label({style_class: 'minimon-foot',
                x_align: Clutter.ActorAlign.END});
            row.add_child(l);
            row.add_child(r);
            grid.add_child(row);
            this._foot.push([l, r]);
        }
        this._card.add_child(grid);

        const chead = new St.BoxLayout({style: 'padding-top: 6px;'});
        chead.add_child(new St.Label({text: 'CLAUDE CODE',
            style_class: 'minimon-host', x_expand: true,
            x_align: Clutter.ActorAlign.START}));
        this._cstat = new St.Label({style_class: 'minimon-foot'});
        chead.add_child(this._cstat);
        this._card.add_child(chead);

        this._usageBox = new St.BoxLayout({vertical: true, style: 'spacing: 8px; padding-top: 4px;'});
        this._card.add_child(this._usageBox);
        this._usageRows = new Map();

        this._today = new St.Label({style_class: 'minimon-foot',
            style: 'padding-top: 4px;', text: 'today  --'});
        this._card.add_child(this._today);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const launch = new PopupMenu.PopupMenuItem('Open floating widget');
        launch.connect('activate', () => {
            if (this._widgetPath)
                GLib.spawn_command_line_async(`python3 ${GLib.shell_quote(this._widgetPath)}`);
        });
        this.menu.addMenuItem(launch);
    }

    _read() {
        try {
            const [ok, bytes] = GLib.file_get_contents(STATUS);
            if (!ok)
                return null;
            const d = JSON.parse(new TextDecoder().decode(bytes));
            if (Date.now() / 1000 - d.ts > 15)
                return null;   // daemon stopped; snapshot is stale
            return d;
        } catch {
            return null;
        }
    }

    _refresh() {
        const d = this._read();
        if (!d) {
            this._label.text = 'minimon: no daemon';
            this._cstat.text = '';
            return;
        }
        this._widgetPath = d.widget;
        const [used, total] = d.ram;
        const memPct = 100 * used / total;
        const quota = [];
        for (const [key, , pct] of (d.claude?.rows || [])) {
            let tag = null;
            if (key.startsWith('session') || key === 'five_hour')
                tag = 'S';
            else if (key === 'weekly_all' || key === 'seven_day')
                tag = 'W';
            else if (key.startsWith('weekly_scoped:'))
                tag = key.split(':')[1][0].toUpperCase();
            if (tag)
                quota.push(`${tag}${Math.round(pct)}%`);
        }
        const gpuT = d.gpu_t ? ` ${Math.round(d.gpu_t)}°` : '';
        const ramT = d.ram_t ? ` ${Math.round(d.ram_t)}°` : '';
        this._label.text =
            `C${Math.round(d.cpu)}% ${d.cpu_t ? Math.round(d.cpu_t) : '--'}° · ` +
            `G${Math.round(d.gpu)}%${gpuT} · M${Math.round(memPct)}%${ramT} · ` +
            (quota.length ? quota.join(' ') : 'CC --');

        this._host.text = d.host || '';
        this._cpu.update(d.cpu / 100, `${Math.round(d.cpu)}%`,
            d.cpu_t ? `${Math.round(d.cpu_t)}°C` : '', d.cpu_t);
        this._gpu.update(d.gpu / 100, `${Math.round(d.gpu)}%`,
            d.gpu_t ? `${Math.round(d.gpu_t)}°C` : '', d.gpu_t);
        this._ram.update(used / total, `${Math.round(memPct)}%`,
            (d.ram_t ? `${Math.round(d.ram_t)}°C · ` : '') +
            `${used.toFixed(1)}/${Math.round(total)} GB`, d.ram_t || null);

        const [[ghzL, ghzR], [vramL, vramR], [netL, netR]] = this._foot;
        ghzL.text = d.ghz ? `${d.ghz.toFixed(1)} GHz` : '';
        ghzR.text = d.gpu_w ? `${Math.round(d.gpu_w)} W` : '';
        vramL.text = d.vram ? `VRAM ${Math.round(d.vram[0])}M` : '';
        vramR.text = d.disk_t ? `SSD ${Math.round(d.disk_t)}°C` : '';
        netL.text = `↓ ${fmtRate(d.net[0])}/s`;
        netR.text = `↑ ${fmtRate(d.net[1])}/s`;

        const cl = d.claude || {};
        this._cstat.text = cl.err || '';
        const seen = new Set();
        const fills = ['minimon-fill-u0', 'minimon-fill-u1', 'minimon-fill-u2'];
        (cl.rows || []).forEach(([key, label, pct, resets], i) => {
            seen.add(key);
            let row = this._usageRows.get(key);
            if (!row) {
                row = new MeterRow(label, fills[Math.min(i, fills.length - 1)]);
                this._usageRows.set(key, row);
                this._usageBox.add_child(row.box);
            }
            row.update(pct / 100, `${Math.round(pct)}%`, resets || '', pct);
        });
        for (const [key, row] of this._usageRows) {
            if (!seen.has(key) && (cl.rows || []).length) {
                row.box.destroy();
                this._usageRows.delete(key);
            }
        }
        const t = cl.today || {};
        this._today.text =
            `today  ${fmtCount(t.calls || 0)} calls · ${fmtCount(t.out || 0)} out tok`;
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = null;
        }
        super.destroy();
    }
});

export default class MinimonExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea('minimon', this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
