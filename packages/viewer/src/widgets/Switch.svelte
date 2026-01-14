<!-- Copyright (c) 2025 Apple Inc. Licensed under MIT License. -->
<script lang="ts">
  interface Props {
    value?: boolean;
    label?: string;
    onChange?: (value: boolean) => void;
    disabled?: boolean;
    class?: string;
  }

  let { value = $bindable(false), label = "", onChange, disabled = false, class: className = "" }: Props = $props();

  const id = `switch-${Math.random().toString(36).slice(2, 9)}`;

  function toggle() {
    if (disabled) return;
    value = !value;
    onChange?.(value);
  }
</script>

<div class="flex items-center gap-2 {className}" class:opacity-50={disabled}>
  <button
    id={id}
    type="button"
    role="switch"
    aria-checked={value}
    aria-label={label || "Toggle"}
    disabled={disabled}
    onclick={toggle}
    class="relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-blue-600 focus:ring-offset-2 {value
      ? 'bg-blue-600'
      : 'bg-slate-200 dark:bg-slate-700'}"
  >
    <span
      aria-hidden="true"
      class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out {value
        ? 'translate-x-5'
        : 'translate-x-0'}"
    ></span>
  </button>
  {#if label}
    <label for={id} class="text-sm text-slate-700 dark:text-slate-300 select-none cursor-pointer">
      {label}
    </label>
  {/if}
</div>
