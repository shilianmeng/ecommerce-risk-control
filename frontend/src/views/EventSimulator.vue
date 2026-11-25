<template>
  <div class="page">
    <div class="grid g2">
      <el-card header="模拟业务系统上报事件（POST /api/v1/events）">
        <el-form :model="ev" label-width="90px" size="default">
          <el-form-item label="事件类型">
            <el-radio-group v-model="ev.event_type">
              <el-radio-button v-for="(v, k) in EVENT_TYPES" :key="k" :value="k">{{ v }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="账号 ID"><el-input v-model="ev.user_id" /></el-form-item>
          <el-form-item label="设备指纹"><el-input v-model="ev.device_id" /></el-form-item>
          <el-form-item label="IP"><el-input v-model="ev.ip" /></el-form-item>
          <el-form-item label="手机号"><el-input v-model="ev.phone" /></el-form-item>
          <el-form-item label="收货地址"><el-input v-model="ev.address" /></el-form-item>
          <el-form-item v-if="needOrder" label="订单号"><el-input v-model="ev.order_id" /></el-form-item>
          <el-form-item v-if="needOrder" label="金额"><el-input-number v-model="ev.amount" :min="0" /></el-form-item>
          <el-form-item v-if="ev.event_type === 'after_sale_apply'" label="退款原因">
            <el-select v-model="extra.refund_reason">
              <el-option label="未收到货 not_received" value="not_received" />
              <el-option label="质量问题 quality" value="quality" />
              <el-option label="不想要了 no_reason" value="no_reason" />
            </el-select>
          </el-form-item>
          <el-form-item label="注册天数"><el-input-number v-model="extra.account_age_days" :min="0" /></el-form-item>
          <el-form-item>
            <div class="btns">
              <el-button type="primary" icon="Promotion" :loading="sending" @click="send()">发送 1 次</el-button>
              <el-button :loading="sending" @click="burst">连发 6 次（换订单号/时间）</el-button>
              <el-button @click="resend" :disabled="!lastBody">重放上一次（验证幂等）</el-button>
            </div>
          </el-form-item>
        </el-form>
        <div class="muted">提示：同一设备连续下单 6 次会触发 R-004；用同一设备换 5 个账号登录会触发 R-012。</div>
      </el-card>
      <el-card header="实时决策结果">
        <el-empty v-if="!results.length" description="发送一次事件看看" />
        <el-timeline>
          <el-timeline-item v-for="(r, i) in results" :key="i" :timestamp="r.at" placement="top"
                            :type="(({ PASS: 'success', REVIEW: 'warning', REJECT: 'danger' }) as any)[r.decision]">
            <el-tag :type="DECISION_TAG[r.decision].type">{{ DECISION_TAG[r.decision].text }}</el-tag>
            <b style="margin: 0 8px">{{ r.score }} 分</b>
            <el-tag v-if="r.duplicate" type="info" size="small">重复事件·返回首次结果</el-tag>
            <span class="muted"> {{ EVENT_TYPES[r.event_type] }} · {{ r.user_id }} · {{ r.latency_ms }}ms</span>
            <div style="margin-top: 4px">
              <el-tag v-for="h in r.hits" :key="h.code" size="small" style="margin-right: 4px">{{ h.code }} {{ h.name }} +{{ h.score }}</el-tag>
            </div>
          </el-timeline-item>
        </el-timeline>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { post } from '@/api/http'
import { DECISION_TAG, EVENT_TYPES } from '@/api/dict'

const rnd = () => Math.random().toString(36).slice(2, 8)
const ev = reactive({
  event_type: 'order_create', user_id: 'demo_' + rnd(), device_id: 'dev-demo-' + rnd(), ip: '58.33.12.' + Math.floor(Math.random() * 250),
  phone: '1380000' + Math.floor(1000 + Math.random() * 8999), address: '上海市徐汇区漕溪北路 100 号', order_id: '', amount: 299,
})
const extra = reactive({ refund_reason: 'not_received', account_age_days: 3 })
const needOrder = computed(() => ['order_create', 'order_pay', 'after_sale_apply'].includes(ev.event_type))
const results = ref<any[]>([])
const sending = ref(false)
const lastBody = ref<any>(null)

function body() {
  const payload: any = { account_age_days: extra.account_age_days }
  if (ev.event_type === 'after_sale_apply') Object.assign(payload, { refund_reason: extra.refund_reason, refund_amount: ev.amount })
  if (ev.event_type === 'coupon_receive') payload.coupon_amount = 20
  return {
    ...ev, order_id: needOrder.value ? ev.order_id || 'SIM' + Date.now() + rnd() : null,
    ts: Date.now() / 1000, payload,
  }
}
async function send(b = body()) {
  sending.value = true
  try {
    const r = await post('/events', b)
    lastBody.value = b
    results.value.unshift({ ...r, at: new Date().toLocaleTimeString() })
  } finally { sending.value = false }
}
async function burst() {
  for (let i = 0; i < 6; i++) await send({ ...body(), order_id: needOrder.value ? 'SIM' + Date.now() + rnd() : null })
}
const resend = () => send(lastBody.value)
</script>
<style scoped>
.btns { display: flex; flex-wrap: wrap; gap: 8px; }
.btns .el-button { margin-left: 0; }
</style>
