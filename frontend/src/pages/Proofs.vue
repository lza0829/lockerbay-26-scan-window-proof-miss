<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const reclaim = ref(null)
const winProof = ref(null)
onMounted(async () => {
  proofs.value = await api.proofs()
  reclaim.value = await api.reclaimProof()
  winProof.value = await api.reclaimWindowProof()
})
</script>
<template>
  <div data-plant="26-scan-window-proof-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">占用成员核对；回收边与回收水位汇总，见下方回收水位核对。</p>
    <div class="panel" style="overflow:auto">
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>关闭</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td>{{ p.closed_at ? p.closed_at : '—' }}</td>
            <td><code>{{ p.event_ids_json }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="reclaim">
      <h2 style="margin:0 0 .5rem;font-size:1rem">回收水位核对（只读）</h2>
      <table>
        <tbody>
          <tr><th>扫描器</th><td><code>{{ reclaim.scanner_name }}</code></td></tr>
          <tr><th>水位</th><td><code>{{ reclaim.watermark ? reclaim.watermark : '—' }}</code></td></tr>
          <tr><th>末批</th><td><code>{{ reclaim.last_batch_id ? reclaim.last_batch_id : '—' }}</code> / 关闭 {{ reclaim.last_closed_count }} 单</td></tr>
        </tbody>
      </table>
      <h2 style="margin:1rem 0 .5rem;font-size:1rem">末批成员占用</h2>
      <p v-if="!reclaim.members.length" class="sub">尚无成功回收批。</p>
      <table v-else>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>回收时刻</th><th>回收事件</th></tr></thead>
        <tbody>
          <tr v-for="m in reclaim.members" :key="m.occupancy_id">
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

    <div class="panel" style="margin-top:1rem" v-if="winProof">
      <h2 style="margin:0 0 .5rem;font-size:1rem">回收扫描窗口核证（只读）</h2>
      <p class="sub" style="margin:0 0 .5rem">
        总体：<span :style="{ color: winProof.ok ? '#86efac' : '#fca5a5' }">
          {{ winProof.ok ? '✓ 窗口链与水位互证通过、成员均落在窗口内' : '✗ 存在违例，见逐项检查' }}
        </span>
        （{{ winProof.window_count }} 趟窗口）
      </p>
      <table>
        <thead><tr><th>检查</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="c in winProof.checks" :key="c.name">
            <td><code>{{ c.name }}</code></td>
            <td :style="{ color: c.ok ? '#86efac' : '#fca5a5' }">{{ c.ok ? '✓' : '✗' }}</td>
            <td>{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>

      <h2 style="margin:1rem 0 .5rem;font-size:1rem">窗口链（每趟可查）</h2>
      <p v-if="!winProof.windows.length" class="sub">尚无成功扫描批，窗口链为空。</p>
      <table v-else>
        <thead><tr><th>批</th><th>窗口区间 (from, to]</th><th>成员数</th><th>成员 ∈ 窗口</th></tr></thead>
        <tbody>
          <tr v-for="w in winProof.windows" :key="w.batch_id">
            <td><code>{{ w.batch_id }}</code></td>
            <td><code>{{ w.from_watermark ? w.from_watermark : '—' }}</code> → <code>{{ w.to_watermark }}</code></td>
            <td>{{ w.member_count }}</td>
            <td :style="{ color: w.members_in_window ? '#86efac' : '#fca5a5' }">
              {{ w.members_in_window ? '✓' : '✗' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
