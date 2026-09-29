<script setup lang="ts">
const environment = import.meta.env.VITE_APP_ENV ?? 'dev'
</script>

<template>
  <main>
    <h1>LabSafe</h1>
    <p>开发环境，结果不用于安全判断。</p>
    <p>APP_ENV: {{ environment }}</p>
  </main>
</template>
