<template>
  <div class="page">
    <div class="toolbar">
      <el-radio-group v-model="lt" @change="load">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button v-for="(v, k) in LIST_TYPES" :key="k" :value="k">{{ v }}</el-radio-button>
      </el-radio-group>
      <el-button v-if="canEdit" type="primary" icon="Plus" @click="show = true">加入名单</el-button>
      <span class="muted">白名单直接通过 · 黑名单直接拒绝 · 灰名单 +20 分重点观察</span>
    </div>
    <el-card>
      <el-table :data="rows" v-loading="loading">
        <el-table-column label="名单" width="90">
          <template #default="{ row }">
            <el-tag :type="row.list_type === 'black' ? 'danger' : row.list_type === 'white' ? 'success' : 'warning'">{{ LIST_TYPES[row.list_type] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90"><template #default="{ row }">{{ ENTITY_TYPES[row.entity_type] }}</template></el-table-column>
        <el-table-column label="值" prop="value" class-name="mono" />
        <el-table-column label="原因" prop="reason" />
        <el-table-column label="过期时间" width="170"><template #default="{ row }">{{ row.expire_at ? fmtTime(row.expire_at) : '永久' }}</template></el-table-column>
        <el-table-column label="操作人" prop="created_by" width="110" />
        <el-table-column label="加入时间" width="170"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="80">
          <template #default="{ row }">
            <el-popconfirm title="移出名单？" @confirm="remove(row)">
              <template #reference><el-button link type="danger">移出</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
    <el-dialog v-model="show" title="加入名单" width="480px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="名单">
          <el-radio-group v-model="form.list_type">
            <el-radio v-for="(v, k) in LIST_TYPES" :key="k" :value="k">{{ v }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.entity_type"><el-option v-for="(v, k) in ENTITY_TYPES" :key="k" :label="v" :value="k" /></el-select>
        </el-form-item>
        <el-form-item label="值"><el-input v-model="form.value" /></el-form-item>
        <el-form-item label="原因"><el-input v-model="form.reason" /></el-form-item>
        <el-form-item label="过期时间">
          <el-date-picker v-model="form.expire_at" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss" placeholder="不填 = 永久" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="show = false">取消</el-button><el-button type="primary" @click="add">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { del, get, post } from '@/api/http'
import { useAuth } from '@/stores/auth'
import { ENTITY_TYPES, LIST_TYPES, fmtTime } from '@/api/dict'

const auth = useAuth()
const canEdit = computed(() => auth.can('strategist', 'auditor'))
const lt = ref('')
const rows = ref<any[]>([])
const loading = ref(false)
const show = ref(false)
const form = reactive({ list_type: 'black', entity_type: 'device', value: '', reason: '', expire_at: null as string | null })

async function load() {
  loading.value = true
  try { rows.value = await get('/namelists', lt.value ? { list_type: lt.value } : undefined) } finally { loading.value = false }
}
async function add() {
  await post('/namelists', form)
  ElMessage.success('已加入，立即生效')
  show.value = false
  form.value = ''
  form.reason = ''
  load()
}
async function remove(row: any) {
  await del(`/namelists/${row.id}`)
  load()
}
onMounted(load)
</script>