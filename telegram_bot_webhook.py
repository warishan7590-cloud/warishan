#!/usr/bin/env python3
"""
Telegram Spending Tracker Bot - Webhook Version for Railway
Author: Built for Railway deployment with webhook functionality
Features: Ticket system, PostgreSQL database, Flask webhook, Admin controls

SETUP INSTRUCTIONS:
1. Install dependencies: pip install python-telegram-bot flask sqlalchemy psycopg2-binary
2. Set environment variables:
   - TELEGRAM_BOT_TOKEN: Your bot token from BotFather
   - ADMIN_ID: Your Telegram user ID for admin commands
   - DATABASE_URL: PostgreSQL connection string
   - WEBHOOK_URL: Your Railway app URL (e.g., https://your-app.railway.app)
3. Run: python telegram_bot_webhook.py

BOT FEATURES:
- Track spending with "mmk" pattern (e.g., "5000 mmk", "2000mmk")
- Displays user name and ID in all responses
- Ticket system: 1 ticket = 10,000 Ks
- Auto-reset at 10 tickets with carryover
- Commands: /start, /check, /wake, /delete, /status
- Webhook mode for Railway deployment
- PostgreSQL database with automatic migration
"""

import os
import re
import time
import logging
import asyncio
import json
from datetime import datetime
from flask import Flask, request, jsonify
from sqlalchemy import Column, Integer, String, DateTime, create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================================
# DATABASE MODELS
# ================================

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    user_id = Column(String, primary_key=True)  # Telegram user ID as string
    name = Column(String, nullable=False)
    total_spent = Column(Integer, default=0)  # Current cycle spending (resets at 10 tickets)
    lifetime_spent = Column(Integer, default=0)  # Total lifetime spending (never resets)
    tickets = Column(Integer, default=0)      # Number of tickets earned
    leftover = Column(Integer, default=0)     # Leftover amount
    created_at = Column(DateTime, default=datetime.utcnow)
    last_transaction = Column(DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<User(user_id='{self.user_id}', name='{self.name}', total_spent={self.total_spent}, tickets={self.tickets})>"

# ================================
# DATABASE MANAGER
# ================================

class DatabaseManager:
    def __init__(self, database_url):
        self.database_url = database_url
        self.engine = None
        self.SessionLocal = None
        self.init_database()
        
    def init_database(self):
        """Initialize database tables with proper error handling"""
        try:
            # Create engine with robust connection settings
            self.engine = create_engine(
                self.database_url,
                pool_pre_ping=True,
                pool_recycle=300,
                pool_timeout=20,
                pool_size=5,
                max_overflow=10,
                echo=False
            )
            self.SessionLocal = sessionmaker(bind=self.engine)
            
            # Test connection
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            
            # Create tables
            Base.metadata.create_all(self.engine)
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def get_session(self):
        """Get database session with retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                session = self.SessionLocal()
                # Test the connection
                session.execute(text("SELECT 1"))
                return session
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    logger.error(f"Failed to get database session after {max_retries} attempts: {e}")
                    raise
                logger.warning(f"Database connection attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(2 ** attempt)  # Exponential backoff

    def add_amount(self, user_id, name, amount):
        """Add amount for user and return result with comprehensive error handling"""
        session = None
        try:
            # Validate inputs
            if not user_id or not name or amount is None:
                raise ValueError("Invalid input parameters")
            
            if not isinstance(amount, int) or amount <= 0:
                raise ValueError(f"Invalid amount: {amount}")
            
            if amount > 1000000000:  # 1 billion limit
                raise ValueError("Amount too large")
                
            session = self.get_session()
            
            # Get or create user
            user = session.query(User).filter(User.user_id == str(user_id)).first()
            if not user:
                user = User(
                    user_id=str(user_id),
                    name=str(name)[:100],  # Limit name length
                    total_spent=0,
                    lifetime_spent=0,
                    tickets=0,
                    leftover=0
                )
                session.add(user)
                logger.info(f"Created new user: {user_id} ({name})")
            
            # Update user data
            user.name = str(name)[:100]  # Update name in case it changed
            user.last_transaction = datetime.utcnow()
            
            # Calculate new tickets from leftover + new amount
            combined = user.leftover + amount
            new_tickets = combined // 10000
            new_leftover = combined % 10000
            
            # Update totals
            user.total_spent += amount
            user.lifetime_spent += amount  # Always track lifetime spending
            user.tickets += new_tickets
            user.leftover = new_leftover
            
            reset_occurred = False
            if user.tickets >= 10:
                # Calculate how many tickets over 10 we have
                excess_tickets = user.tickets - 10
                # Convert excess tickets back to leftover amount
                excess_amount = excess_tickets * 10000
                # New leftover is the remainder + excess amount converted back
                user.leftover = user.leftover + excess_amount
                
                # Calculate new tickets from the total leftover after reset
                user.tickets = user.leftover // 10000
                user.leftover = user.leftover % 10000
                
                # Reset total spent to 0 (this tracks spending since last reset)
                user.total_spent = 0
                reset_occurred = True
                logger.info(f"User {user_id} reached 10+ tickets, resetting with carryover")
            
            session.commit()
            
            result = {
                'total_spent': user.total_spent,
                'lifetime_spent': user.lifetime_spent,
                'tickets': user.tickets,
                'leftover': user.leftover,
                'reset_occurred': reset_occurred
            }
            
            logger.info(f"Successfully processed amount {amount} for user {user_id}")
            return result
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"Error adding amount for user {user_id}: {e}")
            raise
        finally:
            if session:
                session.close()

    def get_user_data(self, user_id):
        """Get user data by ID"""
        session = None
        try:
            session = self.get_session()
            user = session.query(User).filter(User.user_id == str(user_id)).first()
            if not user:
                return None
                
            return {
                'total_spent': user.total_spent,
                'lifetime_spent': user.lifetime_spent,
                'tickets': user.tickets,
                'leftover': user.leftover,
                'name': user.name,
                'created_at': user.created_at.isoformat(),
                'last_transaction': user.last_transaction.isoformat()
            }
        except Exception as e:
            logger.error(f"Error getting user data for {user_id}: {e}")
            return None
        finally:
            if session:
                session.close()

    def subtract_amount(self, user_id, amount):
        """Subtract amount from user (admin function)"""
        session = None
        try:
            if not isinstance(amount, int) or amount <= 0:
                raise ValueError(f"Invalid amount: {amount}")
                
            session = self.get_session()
            user = session.query(User).filter(User.user_id == str(user_id)).first()
            if not user:
                return None

            # Calculate current total value (tickets + leftover)
            current_total = (user.tickets * 10000) + user.leftover
            
            # Subtract the amount from current total
            new_total = max(0, current_total - amount)
            
            # Recalculate tickets and leftover
            user.tickets = new_total // 10000
            user.leftover = new_total % 10000
            
            # Also subtract from total_spent and lifetime_spent (but not below 0)
            user.total_spent = max(0, user.total_spent - amount)
            user.lifetime_spent = max(0, user.lifetime_spent - amount)
            
            user.last_transaction = datetime.utcnow()

            session.commit()

            return {
                'name': user.name,
                'total_spent': user.total_spent,
                'lifetime_spent': user.lifetime_spent,
                'tickets': user.tickets,
                'leftover': user.leftover
            }
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"Error subtracting amount for user {user_id}: {e}")
            raise
        finally:
            if session:
                session.close()

    def get_system_stats(self):
        """Get system statistics"""
        session = None
        try:
            session = self.get_session()
            total_users = session.query(User).count()
            
            # Use safer query methods
            users = session.query(User).all()
            total_amount_sum = sum(user.total_spent for user in users)
            total_tickets_sum = sum(user.tickets for user in users)
            lifetime_sum = sum(user.lifetime_spent for user in users)
            
            return {
                'total_users': total_users,
                'total_amount': total_amount_sum,
                'total_tickets': total_tickets_sum,
                'lifetime_total': lifetime_sum,
                'database_type': 'PostgreSQL',
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            logger.error(f"Error getting system stats: {e}")
            return {
                'total_users': 0,
                'total_amount': 0,
                'total_tickets': 0,
                'lifetime_total': 0,
                'database_type': 'PostgreSQL',
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'error': str(e)
            }
        finally:
            if session:
                session.close()

# ================================
# BOT HANDLERS
# ================================

class BotHandlers:
    def __init__(self, admin_id, db_manager):
        self.admin_id = str(admin_id) if admin_id else None
        self.db_manager = db_manager
        # Improved regex pattern for better matching
        self.amount_pattern = re.compile(r'(\d+)\s*mmk', re.IGNORECASE)

    async def handle_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount messages with 'mmk' pattern - Enhanced with user info"""
        try:
            # Validate update and message
            if not update or not update.message or not update.message.text:
                logger.warning("Invalid update or message received")
                return
                
            if not update.message.from_user:
                logger.warning("Message without user information")
                return

            user_id = str(update.message.from_user.id)
            name = update.message.from_user.first_name or "User"
            text = update.message.text.strip()
            
            logger.info(f"Processing message from user {user_id} ({name}): '{text}'")

            # Use compiled regex pattern for better performance and reliability
            match = self.amount_pattern.search(text)
            if not match:
                logger.debug(f"No amount pattern found in message: '{text}'")
                return  # Ignore if not matching pattern

            # Extract and validate amount
            try:
                amount_str = match.group(1)
                amount = int(amount_str)
                
                if amount <= 0:
                    await update.message.reply_text("❌ Amount must be greater than 0!")
                    return
                    
                if amount > 1000000000:  # 1 billion limit
                    await update.message.reply_text("❌ Amount is too large!")
                    return
                    
            except (ValueError, OverflowError) as e:
                logger.error(f"Invalid amount format in '{text}': {e}")
                await update.message.reply_text("❌ Invalid amount format!")
                return

            logger.info(f"Processing amount {amount} for user {user_id} ({name})")

            # Process the amount with database
            try:
                result = self.db_manager.add_amount(user_id, name, amount)
                
                # Format response message with user name and ID
                response_parts = []
                response_parts.append(f"✅ Added {amount:,} Ks")
                
                if result['reset_occurred']:
                    response_parts.append("🎉 Congratulations! You reached 10 tickets!")
                    response_parts.append("🔄 Reset completed with carryover applied")
                
                response_parts.append(f"📊 Total: {result['total_spent']:,} Ks")
                response_parts.append(f"💰 Lifetime: {result['lifetime_spent']:,} Ks")
                response_parts.append(f"🎫 Tickets: {result['tickets']}")
                response_parts.append(f"💵 Leftover: {result['leftover']:,} Ks")
                response_parts.append(f"🆔 ID: {user_id}")
                response_parts.append(f"😊 Thank you, {name}!")
                
                response_message = "\n".join(response_parts)
                
                await update.message.reply_text(response_message)
                logger.info(f"Successfully processed amount {amount} for user {user_id}")
                
            except Exception as db_error:
                logger.error(f"Database error processing amount for user {user_id}: {db_error}")
                await update.message.reply_text(
                    "❌ Sorry, there was an error processing your transaction. Please try again later."
                )
                
        except Exception as e:
            logger.error(f"Unexpected error in handle_amount: {e}", exc_info=True)
            try:
                if update and update.message:
                    await update.message.reply_text(
                        "❌ An unexpected error occurred. Please try again later."
                    )
            except:
                pass  # Don't fail if we can't even send error message

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        try:
            user_name = update.message.from_user.first_name or "User"
            user_id = str(update.message.from_user.id)
            
            message = f"""👋 Welcome {user_name}!

This is your **Spending Tracker Bot**. Here's how to use it:

💰 **Track Spending:**
Just type any amount followed by 'mmk'
Examples: `2000mmk`, `5000 mmk`, `500mmk`

🎫 **Ticket System:**
• 1 ticket = 10,000 Ks
• Automatic reset at 10 tickets with carryover
• Your progress carries over to next cycle

📋 **Commands:**
• `/check` - View your spending summary
• `/status` - Bot system status
• `/wake` - Wake up the bot

🆔 Your ID: {user_id}

Start tracking by sending a message like: `2000mmk`
"""
            
            await update.message.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in start_command: {e}")
            await update.message.reply_text("Welcome! Send amounts like '2000mmk' to track spending.")

    async def check_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check spending command"""
        try:
            user_id = str(update.message.from_user.id)
            user_name = update.message.from_user.first_name or "User"
            
            user_data = self.db_manager.get_user_data(user_id)
            
            if not user_data:
                await update.message.reply_text("No spending data found. Start by sending an amount like '2000mmk'!")
                return
            
            message = f"""📋 {user_name}, here is your status:

📊 Total: {user_data['total_spent']:,} Ks
💰 Lifetime: {user_data['lifetime_spent']:,} Ks
🎫 Tickets: {user_data['tickets']}
💵 Leftover: {user_data['leftover']:,} Ks
🆔 ID: {user_id}

📅 Last transaction: {user_data['last_transaction'][:19].replace('T', ' ')}
"""
            
            await update.message.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in check_command: {e}")
            await update.message.reply_text("❌ Error retrieving your data. Please try again later.")

    async def wake_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Wake command handler"""
        try:
            user_name = update.message.from_user.first_name or "User"
            
            message = f"""✅ Bot is awake and active!
🔗 Webhook mode enabled
⚡ Ready to track spending
🕐 Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Thanks for keeping me running, {user_name}!"""
            
            await update.message.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in wake_command: {e}")
            await update.message.reply_text("🤖 Bot is awake!")

    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete/subtract amount command (admin only)"""
        try:
            user_id = str(update.message.from_user.id)
            
            # Check if user is admin
            if not self.admin_id or user_id != self.admin_id:
                await update.message.reply_text("❌ This command is only available to administrators.")
                return
            
            # Parse command arguments
            if not context.args or len(context.args) < 2:
                await update.message.reply_text(
                    "❌ Usage: `/delete <user_id> <amount>`\n"
                    "Example: `/delete 123456789 5000`",
                    parse_mode='Markdown'
                )
                return
            
            try:
                target_user_id = context.args[0]
                amount = int(context.args[1])
                
                if amount <= 0:
                    await update.message.reply_text("❌ Amount must be greater than 0!")
                    return
                    
            except ValueError:
                await update.message.reply_text("❌ Invalid amount format!")
                return
            
            # Subtract amount
            result = self.db_manager.subtract_amount(target_user_id, amount)
            
            if not result:
                await update.message.reply_text(f"❌ User {target_user_id} not found!")
                return
            
            message = f"""✅ Subtracted {amount:,} Ks from {result['name']}.

📊 Total: {result['total_spent']:,} Ks
💰 Lifetime: {result['lifetime_spent']:,} Ks
🎫 Tickets: {result['tickets']}
💵 Leftover: {result['leftover']:,} Ks
"""
            
            await update.message.reply_text(message)
            logger.info(f"Admin {user_id} deleted {amount} from user {target_user_id}")
            
        except Exception as e:
            logger.error(f"Error in delete_command: {e}")
            await update.message.reply_text("❌ Error processing delete command. Please try again later.")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Status command handler (admin only)"""
        try:
            user_id = str(update.message.from_user.id)
            
            # Check if user is admin
            if not self.admin_id or user_id != self.admin_id:
                await update.message.reply_text("❌ This command is only available to administrators.")
                return
            
            stats = self.db_manager.get_system_stats()
            
            message = f"""🤖 **System Status:**

📊 **Database Statistics:**
• Total Users: {stats['total_users']}
• Total Amount: {stats['total_amount']:,} Ks
• Total Tickets: {stats['total_tickets']}
• Database: {stats['database_type']}

⚡ **Server Status:**
• Status: ✅ Running (Webhook Mode)
• Mode: Railway Deployment
• Current Time: {datetime.now().strftime('%H:%M:%S')}

📅 Last Updated: {stats['last_updated']}
"""
            
            await update.message.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in status_command: {e}")
            await update.message.reply_text("❌ Error retrieving system status. Please try again later.")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Log Errors caused by Updates."""
        logger.error(f'Update {update} caused error {context.error}')

# ================================
# FLASK WEBHOOK SERVER
# ================================

# Global variables
bot_application = None
bot_handlers = None
db_manager = None
bot_start_time = time.time()

# Flask app
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

@app.route('/')
def home():
    """Health check endpoint"""
    try:
        uptime = time.time() - bot_start_time
        return jsonify({
            'status': 'alive',
            'service': 'Telegram Spending Tracker Bot - Webhook',
            'uptime_seconds': int(uptime),
            'mode': 'webhook',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    """Detailed health check"""
    try:
        uptime = time.time() - bot_start_time
        stats = db_manager.get_system_stats() if db_manager else {'error': 'Database not initialized'}
        
        return jsonify({
            'healthy': True,
            'uptime': int(uptime),
            'mode': 'webhook',
            'database_status': 'connected' if 'error' not in stats else 'error',
            'stats': stats,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'healthy': False, 'error': str(e)}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Telegram webhook"""
    try:
        if not bot_application:
            logger.error("Bot application not initialized")
            return jsonify({'error': 'Bot not initialized'}), 500
            
        # Get the update data
        update_data = request.get_json()
        if not update_data:
            logger.warning("No JSON data received in webhook")
            return jsonify({'error': 'No data'}), 400
            
        logger.debug(f"Received webhook update: {json.dumps(update_data, indent=2)}")
        
        # Create Update object and process it
        update = Update.de_json(update_data, bot_application.bot)
        
        # Process the update asynchronously
        asyncio.create_task(process_update(update))
        
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return jsonify({'error': 'Processing failed'}), 500

async def process_update(update: Update):
    """Process a single update"""
    try:
        if not update.message:
            logger.debug("Update has no message, skipping")
            return
            
        message = update.message
        
        # Handle commands
        if message.text and message.text.startswith('/'):
            command = message.text.split()[0].lower()
            
            if command == '/start':
                await bot_handlers.start_command(update, None)
            elif command == '/check':
                await bot_handlers.check_command(update, None)
            elif command == '/status':
                await bot_handlers.status_command(update, None)
            elif command == '/wake':
                await bot_handlers.wake_command(update, None)
            elif command == '/delete':
                await bot_handlers.delete_command(update, None)
            else:
                await update.message.reply_text("Unknown command. Use /start for help.")
        
        # Handle spending messages
        elif message.text:
            await bot_handlers.handle_amount(update, None)
            
    except Exception as e:
        logger.error(f"Error processing update: {e}")

@app.route('/set_webhook', methods=['POST'])
def set_webhook():
    """Set webhook URL"""
    try:
        data = request.get_json()
        webhook_url = data.get('webhook_url') if data else None
        
        if not webhook_url:
            return jsonify({'error': 'webhook_url required'}), 400
            
        # Set webhook
        result = asyncio.run(bot_application.bot.set_webhook(url=webhook_url))
        
        if result:
            logger.info(f"Webhook set to: {webhook_url}")
            return jsonify({'status': 'success', 'webhook_url': webhook_url})
        else:
            return jsonify({'error': 'Failed to set webhook'}), 500
            
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/stats')
def stats():
    """Get bot statistics"""
    try:
        if not db_manager:
            return jsonify({'error': 'Database not initialized'}), 500
            
        stats = db_manager.get_system_stats()
        return jsonify(stats)
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({'error': str(e)}), 500

# ================================
# MAIN APPLICATION
# ================================

def validate_environment():
    """Validate required environment variables"""
    required_vars = ['TELEGRAM_BOT_TOKEN', 'DATABASE_URL']
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please set the following environment variables:")
        logger.error("- TELEGRAM_BOT_TOKEN: Your bot token from BotFather")
        logger.error("- DATABASE_URL: PostgreSQL connection string")
        logger.error("- ADMIN_ID: Your Telegram user ID for admin commands (optional)")
        logger.error("- WEBHOOK_URL: Your Railway app URL (optional, can be set via /set_webhook)")
        return False
    
    return True

def setup_bot():
    """Setup bot application and handlers"""
    global bot_application, bot_handlers, db_manager
    
    try:
        # Get environment variables
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        admin_id = os.getenv('ADMIN_ID')
        database_url = os.getenv('DATABASE_URL')
        webhook_url = os.getenv('WEBHOOK_URL')
        
        # Initialize database
        logger.info("Initializing database...")
        db_manager = DatabaseManager(database_url)
        
        # Initialize bot handlers
        logger.info("Setting up bot handlers...")
        bot_handlers = BotHandlers(admin_id, db_manager)
        
        # Create application
        bot_application = Application.builder().token(bot_token).build()
        
        # Set webhook if URL provided
        if webhook_url:
            logger.info(f"Setting webhook to: {webhook_url}")
            result = asyncio.run(bot_application.bot.set_webhook(url=f"{webhook_url}/webhook"))
            if result:
                logger.info("Webhook set successfully")
            else:
                logger.warning("Failed to set webhook")
        else:
            logger.warning("No WEBHOOK_URL provided. Use /set_webhook endpoint to configure.")
        
        logger.info("Bot setup completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error setting up bot: {e}")
        return False

if __name__ == "__main__":
    logger.info("Starting Telegram Spending Tracker Bot - Webhook Mode...")
    
    # Validate environment
    if not validate_environment():
        exit(1)
    
    # Setup bot
    if not setup_bot():
        exit(1)
    
    # Start Flask server
    port = int(os.getenv('PORT', 5000))
    logger.info(f"Starting Flask server on port {port}...")
    
    app.run(host='0.0.0.0', port=port, debug=False)