<template>
  <div class="page">
    <div class="grid" style="grid-template-columns: 380px 1fr">
      <el-card>
        <template #header>
          团伙识别
          <el-input-number v-model="minSize" :min="2" :max="50" size="small" style="width: 100px; margin-left: 8px" @change="loadGangs" />
          <span class="muted"> 人以上</span>
          <div class="muted" style="font-size: 12px; margin-top: 4px">共用设备 / IP / 手机 / 地址的连通分量</div>
        </template>
        <div class="muted" style="margin-bottom: 8px">图谱后端：{{ backend }}</div>
        <el-empty v-if="!gangs.length" description="没有达到规模的团伙" />
        <el-card v-for="g in gangs" :key="g.gang_id" shadow="hover" class="gang" :class="{ on: g.gang_id === cur }" @click="pickGang(g)">
          <div style="display: flex; justify-content: space-between">
            <b>{{ g.gang_id }}</b><el-tag type="danger">{{ g.size }} 个账号</el-tag>
          </div>
          <div class="muted mono" style="margin: 6px 0; font-size: 12px">{{ g.accounts.slice(0, 8).join('、') }}{{ g.size > 8 ? ' …' : '' }}</div>
          <div>
            <el-tag v-for="e in g.shared_entities.slice(0, 4)" :key="e.type + e.value" size="small" type="warning" style="margin: 2px">
              {{ ENT_CN[e.type] || e.type }} {{ e.value }} ×{{ e.degree }}
            </el-tag>
          </div>
        </el-card>
      </el-card>
      <el-card>
        <template #header>
          <el-input v-model="user" placeholder="输入账号 ID 查关联" style="width: 220px" @keyup.enter="loadUser" />
          <el-select v-model="hops" style="width: 90px; margin-left: 8px">
            <el-option :value="1" label="1 跳" /><el-option :value="2" label="2 跳" /><el-option :value="3" label="3 跳" />
          </el-select>
          <el-button type="primary" icon="Search" style="margin-left: 8px" @click="loadUser">查询</el-button>
          <span v-if="nb" class="muted" style="margin-left: 12px">关联账号 {{ nb.related_accounts.length }} 个 · 点击账号节点可继续下钻</span>
        </template>
        <el-empty v-if="!nb" description="选一个团伙或输入账号" />
        <RelationGraph v-else :data="nb" :height="620" @account="(u: string) => { user = u; loadUser() }" />
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { get } from '@/api/http'
import RelationGraph from '@/components/RelationGraph.vue'

const ENT_CN: Record<string, string> = { Device: '设备', IP: 'IP', Phone: '手机', Address: '地址' }
const route = useRoute()
const minSize = ref(3)
const gangs = ref<any[]>([])
const backend = ref('')
const cur = ref('')
const user = ref((route.query.user as string) || '')
const hops = ref(2)
const nb = ref<any>(null)

async function loadGangs() {
  const r = await get('/graph/gangs', { min_size: minSize.value })
  gangs.value = r.gangs
  backend.value = r.backend
}
async function loadUser() {
  if (!user.value) return
  nb.value = await get(`/graph/accounts/${encodeURIComponent(user.value)}`, { hops: hops.value })
}
function pickGang(g: any) {
  cur.value = g.gang_id
  user.value = g.accounts[0]
  loadUser()
}
onMounted(async () => {
  await loadGangs()
  if (user.value) loadUser()
  else if (gangs.value.length) pickGang(gangs.value[0])
})
</script>

<style scoped>
.gang { margin-bottom: 8px; cursor: pointer; }
.gang.on { border-color: #409eff; }
</style>