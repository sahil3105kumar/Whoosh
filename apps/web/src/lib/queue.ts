/**
 * Outgoing file queue: files are sent one at a time, in the order they
 * were added, and can be removed while still waiting (#14).
 *
 * One at a time because concurrent sends would only split the same
 * DataChannel's bandwidth, finishing every file later than in sequence.
 */

export interface QueuedFile {
  readonly id: string;
  readonly file: File;
}

export class SendQueue {
  private items: QueuedFile[] = [];
  private running = false;

  constructor(
    private readonly send: (file: File) => Promise<unknown>,
    private readonly onChange: (waiting: readonly QueuedFile[]) => void = () => {},
  ) {}

  /** Files waiting to be sent (not including the one being sent). */
  get waiting(): readonly QueuedFile[] {
    return this.items;
  }

  add(files: Iterable<File>): void {
    for (const file of files) this.items.push({ id: crypto.randomUUID(), file });
    this.changed();
    void this.run();
  }

  remove(id: string): boolean {
    const before = this.items.length;
    this.items = this.items.filter((item) => item.id !== id);
    if (this.items.length === before) return false;
    this.changed();
    return true;
  }

  /** Drop everything still waiting; returns what was dropped. */
  clear(): QueuedFile[] {
    const dropped = this.items;
    this.items = [];
    if (dropped.length) this.changed();
    return dropped;
  }

  private async run(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      for (let next = this.items.shift(); next; next = this.items.shift()) {
        this.changed();
        try {
          await this.send(next.file);
        } catch (err) {
          // A failed send must not stall the rest of the queue
          console.error(`sending ${next.file.name} failed`, err);
        }
      }
    } finally {
      this.running = false;
    }
  }

  private changed(): void {
    this.onChange([...this.items]);
  }
}
