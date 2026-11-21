<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const rows = ref([])
const proof = ref(null)
const win = ref(null)
const report = ref(null)
const failOn = ref('')
const err = ref('')

async function load() {
  rows.value = await api.occupancies()
  proof.value = await api.reclaimProof()
  win.value = await api.reclaimScanWindow()
}
async function scan() {
  err.value = ''
  report.value = null
  try {
    const body = failOn.value.trim() ? { fail_on: failOn.value.trim() } : {}
    report.value = await api.reclaimScan(body)
  } catch (e) {
    err.value = String(e)
  }
  await load()
}
onMounted(load)
</script>
<template>
  <div data-plant="26-scan-window-proof-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>回收</h1>
    <p class="sub">超时回收按持久水位扫描：本批只处理 opened_at ≤ 水位且已超时的占用；关账、水位前进与扫描窗口证明同一事务固化，失败整批回滚、水位不抬、窗口不留残骸。</p>

    <div class="panel" v-if="proof">
      <h2 style="margin:0 0 .5rem;font-size:1rem">回收水位核对（只读）</h2>
      <table>
        <tbody>
          <tr><th>扫描器</th><td><code>{{ proof.scanner_name }}</code></td></tr>
          <tr><th>当前水位</th><td><code>{{ proof.watermark ? proof.watermark : '—（尚未成功跑过任何一批）' }}</code></td></tr>
          <tr><th>水位推进时刻</th><td>{{ proof.advanced_at ? proof.advanced_at : '—' }}</td></tr>
          <tr><th>末批</th><td><code>{{ proof.last_batch_id ? proof.last_batch_id : '—' }}</code></td></tr>
          <tr><th>本批关闭数</th><td>{{ proof.last_closed_count }}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="win">
      <h2 style="margin:0 0 .5rem;font-size:1rem">本趟扫描窗口证明（只读）</h2>
      <p v-if="!win.complete" class="sub">尚无成功扫描批：水位未抬、窗口未固化。</p>
      <template v-else>
        <table>
          <tbody>
            <tr><th>窗口批次</th><td><code>{{ win.batch_id }}</code></td></tr>
            <tr>
              <th>窗口区间 (from, to]</th>
              <td><code>{{ win.from_watermark ? win.from_watermark : '—（首批，下界 -∞）' }}</code> → <code>{{ win.to_watermark }}</code></td>
            </tr>
            <tr><th>超时闸临界</th><td><code>{{ win.cutoff_ts }}</code>（{{ win.overdue_hours }}h 未取）</td></tr>
            <tr>
              <th>成员占用 ∈ 窗口</th>
              <td :style="{ color: win.members_in_window ? '#86efac' : '#fca5a5' }">
                {{ win.member_count }} 单 {{ win.members_in_window ? '✓ 均落在 (from, to] 内' : '✗ 存在窗口外成员' }}
              </td>
            </tr>
          </tbody>
        </table>
        <p class="sub" style="margin:.5rem 0 0">
          成员占用 id：<code v-for="id in win.member_ids" :key="id" style="margin-right:.5rem">{{ id }}</code>
          <span v-if="!win.member_ids.length">（空窗口：本趟 0 关闭）</span>
        </p>
      </template>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">跑一批扫描</h2>
      <p style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
        <button @click="scan">执行回收扫描</button>
        <label class="sub">夹具注入失败成员 id（留空正常跑）
          <input v-model="failOn" placeholder="如 OCC-T-A02"
            style="margin-left:.5rem;background:#111827;color:#e8eef6;border:1px solid #334155;border-radius:4px;padding:.3rem" />
        </label>
      </p>
      <pre v-if="report" class="panel" :style="{ color: report.status === 'failed' ? '#fca5a5' : '#86efac' }">{{ report }}</pre>
      <p v-if="err" class="sub" style="color:#fca5a5">{{ err }}</p>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="proof?.members?.length">
      <h2 style="margin:0 0 .5rem;font-size:1rem">末批成员占用（{{ proof.members.length }}）</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>回收时刻</th><th>回收事件</th></tr></thead>
        <tbody>
          <tr v-for="m in proof.members" :key="m.occupancy_id">
            <td><code>{{ m.occupancy_id }}</code></td>
            <td>{{ m.cell_id }}</td>
            <td>{{ m.tracking_no }}</td>
            <td>{{ m.opened_at }}</td>
            <td>{{ m.reclaimed_at }}</td>
            <td><code>{{ m.event_id }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="proof?.recent_batches?.length">
      <h2 style="margin:0 0 .5rem;font-size:1rem">批台账（近 10 批）</h2>
      <table>
        <thead><tr><th>批</th><th>状态</th><th>关闭数</th><th>前水位</th><th>后水位</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="b in proof.recent_batches" :key="b.batch_id">
            <td><code>{{ b.batch_id }}</code></td>
            <td :style="{ color: b.status === 'failed' ? '#fca5a5' : '#86efac' }">{{ b.status }}</td>
            <td>{{ b.closed_count }}</td>
            <td><code>{{ b.prev_watermark ? b.prev_watermark : '—' }}</code></td>
            <td><code>{{ b.next_watermark ? b.next_watermark : '—（失败不抬水位）' }}</code></td>
            <td>{{ b.started_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用账</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>状态</th><th>开启</th><th>关闭</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td><code>{{ r.id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.opened_at }}</td>
            <td>{{ r.closed_at ? r.closed_at : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
