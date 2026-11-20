<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const reclaim = ref(null)
onMounted(async () => {
  proofs.value = await api.proofs()
  reclaim.value = await api.reclaimProof()
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
  </div>
</template>
