<template>
  <div class="page">
    <div class="toolbar">
      <el-button v-if="canEdit" type="primary" icon="Plus" @click="openEdit()">新建规则</el-button>
      <span class="muted">规则变更即时热加载；分值相加后按 0-59 通过 / 60-79 人审 / 80-100 拒绝 仲裁</span>
    </div>
    <el-card>
      <el-table :data="rules" v-loading="loading">
        <el-table-column label="编号" prop="code" width="90" />
        <el-table-column label="名称" prop="name" width="200" />
        <el-table-column label="适用事件" width="160">
          <template #default="{ row }">
            <el-tag v-for="t in row.event_types" :key="t" size="small" style="margin: 1px">{{ EVENT_TYPES[t] }}</el-tag>
            <span v-if="!row.event_types.length" class="muted">全部</span>
          </template>
        </el-table-column>
        <el-table-column label="表达式" prop="expression" class-name="mono" show-overflow-tooltip />
        <el-table-column label="分值" width="70">
          <template #default="{ row }"><b :class="row.score > 0 ? 'bad' : 'ok'">{{ row.score > 0 ? '+' : '' }}{{ row.score }}</b></template>
        </el-table-column>
        <el-table-column label="版本" width="60"><template #default="{ row }">v{{ row.version }}</template></el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }"><el-switch :model-value="row.enabled" :disabled="!canEdit" @change="toggle(row)" /></template>
        </el-table-column>
        <el-table-column label="操作" width="130" v-if="canEdit">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-popconfirm :title="`删除规则 ${row.code}？`" @confirm="remove(row)">
              <template #reference><el-button link type="danger">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="show" :title="editing ? `编辑规则 ${form.code}` : '新建规则'" width="860px">
      <el-form :model="form" label-width="80px">
        <div class="grid g2" style="gap: 0 16px">
          <el-form-item label="编号"><el-input v-model="form.code" :disabled="editing" placeholder="如 R-101" /></el-form-item>
          <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
          <el-form-item label="分值"><el-input-number v-model="form.score" :min="-100" :max="100" /></el-form-item>
          <el-form-item label="优先级"><el-input-number v-model="form.priority" :min="0" /></el-form-item>
        </div>
        <el-form-item label="适用事件">
          <el-checkbox-group v-model="form.event_types">
            <el-checkbox v-for="(v, k) in EVENT_TYPES" :key="k" :value="k">{{ v }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="表达式">
          <el-input v-model="form.expression" type="textarea" :rows="3" class="mono"
                    placeholder="device_order_count_1h >= 5 AND is_new_account == true" />
        </el-form-item>
        <el-form-item label="说明"><el-input v-model="form.description" /></el-form-item>
        <el-form-item label="样例特征">
          <el-input v-model="sample" type="textarea" :rows="3" class="mono" placeholder='{"device_order_count_1h": 6, "is_new_account": true}' />
        </el-form-item>
        <el-form-item>
          <el-button @click="check">校验 / 试算</el-button>
          <template v-if="vr">
            <el-tag :type="vr.ok ? 'success' : 'danger'" style="margin-left: 8px">{{ vr.ok ? '语法正确' : vr.msg }}</el-tag>
            <el-tag v-if="vr.matched !== undefined" :type="vr.matched ? 'danger' : 'info'" style="margin-left: 8px">
              样例{{ vr.matched ? '命中' : '未命中' }}
            </el-tag>
          </template>
        </el-form-item>
        <el-table v-if="vr?.conditions" :data="vr.conditions" size="small" style="margin-left: 80px; width: auto">
          <el-table-column label="条件" prop="condition" class-name="mono" />
          <el-table-column label="实际值" width="120"><template #default="{ row }">{{ row.actual ?? 'null' }}</template></el-table-column>
          <el-table-column label="满足" width="70"><template #default="{ row }"><span :class="row.matched ? 'ok' : 'bad'">{{ row.matched ? '✓' : '✗' }}</span></template></el-table-column>
          <el-table-column label="关键" width="70">
            <template #default="{ row }"><el-tag v-if="vr.counterfactual?.critical?.includes(row.cid)" size="small" type="warning">关键</el-tag></template>
          </el-table-column>
        </el-table>
        <div v-if="vr?.counterfactual" style="margin: 8px 0 0 80px; font-size: 13px">
          <el-tag size="small" type="warning" effect="plain">反事实</el-tag>
          <span v-if="vr.counterfactual.minimal_change"> {{ vr.counterfactual.minimal_change.text }}</span>
          <span v-else class="muted"> 没有单个关键条件：多个分支同时成立，改任何一个都不会改变结果</span>
        </div>
      </el-form>
      <el-collapse style="margin-top: 12px">
        <el-collapse-item title="可用特征字段（点击插入）">
          <el-tag v-for="f in feats" :key="f.name" class="feat" @click="form.expression += (form.expression ? ' ' : '') + f.name">
            <span class="mono">{{ f.name }}</span> <span class="muted">{{ f.description }}</span>
          </el-tag>
          <div class="muted" style="margin-top: 8px">运算符：== != &gt; &gt;= &lt; &lt;= IN NOT IN AND OR NOT ( )，字符串用引号，列表用 [ ]</div>
        </el-collapse-item>
      </el-collapse>
      <template #footer>
        <el-button @click="show = false">取消</el-button>
        <el-button type="primary" @click="save">保存并生效</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { del, get, post, put } from '@/api/http'
import { useAuth } from '@/stores/auth'
import { EVENT_TYPES } from '@/api/dict'

const auth = useAuth()
const canEdit = computed(() => auth.can('strategist'))
const rules = ref<any[]>([])
const feats = ref<any[]>([])
const loading = ref(false)
const show = ref(false)
const editing = ref(false)
const blank = () => ({ code: '', name: '', description: '', event_types: [] as string[], expression: '', score: 20, enabled: true, priority: 100 })
const form = ref(blank())
const sample = ref('')
const vr = ref<any>(null)

async function load() {
  loading.value = true
  try { rules.value = await get('/rules') } finally { loading.value = false }
}
function openEdit(row?: any) {
  editing.value = !!row
  form.value = row ? { ...blank(), ...row, event_types: [...row.event_types] } : blank()
  sample.value = ''
  vr.value = null
  show.value = true
}
async function check() {
  let s: any
  if (sample.value.trim()) {
    try { s = JSON.parse(sample.value) } catch { return ElMessage.error('样例特征不是合法 JSON') }
  }
  try {
    vr.value = await post('/rules/validate', { expression: form.value.expression, sample: s }, { silent: true })
  } catch (e: any) {
    vr.value = { ok: false, msg: e?.response?.data?.detail || '校验失败' }
  }
}
async function save() {
  const { code, name, description, event_types, expression, score, enabled, priority } = form.value
  const body = { name, description, event_types, expression, score, enabled, priority }
  if (editing.value) await put(`/rules/${code}`, body)
  else await post('/rules', { code, ...body })
  ElMessage.success('已保存，规则已热加载')
  show.value = false
  load()
}
async function toggle(row: any) {
  await put(`/rules/${row.code}`, { enabled: !row.enabled })
  load()
}
async function remove(row: any) {
  await del(`/rules/${row.code}`)
  ElMessage.success('已删除')
  load()
}
onMounted(async () => {
  load()
  feats.value = await get('/features')
})
</script>

<style scoped>
.feat { margin: 3px; cursor: pointer; }
</style>