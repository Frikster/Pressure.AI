import asyncio
import os
from typing import Any, Dict, List, Optional

import requests
from exa_py import Exa
from langchain_community.retrievers import ArxivRetriever
from langchain_community.utilities.pubmed import PubMedAPIWrapper
from langsmith import traceable
from tavily import AsyncTavilyClient, TavilyClient

from open_deep_research.state import Section

tavily_client = TavilyClient()
tavily_async_client = AsyncTavilyClient()


def get_config_value(value):
    """
    Helper function to handle both string and enum cases of configuration values
    """
    return value if isinstance(value, str) else value.value

# Helper function to get search parameters based on the search API and config
def get_search_params(search_api: str, search_api_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Filters the search_api_config dictionary to include only parameters accepted by the specified search API.

    Args:
        search_api (str): The search API identifier (e.g., "exa", "tavily").
        search_api_config (Optional[Dict[str, Any]]): The configuration dictionary for the search API.

    Returns:
        Dict[str, Any]: A dictionary of parameters to pass to the search function.
    """
    # Define accepted parameters for each search API
    SEARCH_API_PARAMS = {
        "exa": ["max_characters", "num_results", "include_domains", "exclude_domains", "subpages"],
        "tavily": [],  # Tavily currently accepts no additional parameters
        "perplexity": [],  # Perplexity accepts no additional parameters
        "arxiv": ["load_max_docs", "get_full_documents", "load_all_available_meta"],
        "pubmed": ["top_k_results", "email", "api_key", "doc_content_chars_max"],
    }

    # Get the list of accepted parameters for the given search API
    accepted_params = SEARCH_API_PARAMS.get(search_api, [])

    # If no config provided, return an empty dict
    if not search_api_config:
        return {}

    # Filter the config to only include accepted parameters
    return {k: v for k, v in search_api_config.items() if k in accepted_params}

def deduplicate_and_format_sources(search_response, max_tokens_per_source, include_raw_content=True):
    """
    Takes a list of search responses and formats them into a readable string.
    Limits the raw_content to approximately max_tokens_per_source.
 
    Args:
        search_responses: List of search response dicts, each containing:
            - query: str
            - results: List of dicts with fields:
                - title: str
                - url: str
                - content: str
                - score: float
                - raw_content: str|None
        max_tokens_per_source: int
        include_raw_content: bool
            
    Returns:
        str: Formatted string with deduplicated sources
    """
     # Collect all results
    sources_list = []
    for response in search_response:
        sources_list.extend(response['results'])
    
    # Deduplicate by URL
    unique_sources = {source['url']: source for source in sources_list}

    # Format output
    formatted_text = "Sources:\n\n"
    for i, source in enumerate(unique_sources.values(), 1):
        formatted_text += f"Source {source['title']}:\n===\n"
        formatted_text += f"URL: {source['url']}\n===\n"
        formatted_text += f"Most relevant content from source: {source['content']}\n===\n"
        if include_raw_content:
            # Using rough estimate of 4 characters per token
            char_limit = max_tokens_per_source * 4
            # Handle None raw_content
            raw_content = source.get('raw_content', '')
            if raw_content is None:
                raw_content = ''
                print(f"Warning: No raw_content found for source {source['url']}")
            if len(raw_content) > char_limit:
                raw_content = raw_content[:char_limit] + "... [truncated]"
            formatted_text += f"Full source content limited to {max_tokens_per_source} tokens: {raw_content}\n\n"
                
    return formatted_text.strip()

def format_sections(sections: list[Section]) -> str:
    """ Format a list of sections into a string """
    formatted_str = ""
    for idx, section in enumerate(sections, 1):
        formatted_str += f"""
{'='*60}
Section {idx}: {section.name}
{'='*60}
Description:
{section.description}
Requires Research: 
{section.research}

Content:
{section.content if section.content else '[Not yet written]'}

"""
    return formatted_str

@traceable
async def tavily_search_async(search_queries):
    """
    Performs concurrent web searches using the Tavily API.

    Args:
        search_queries (List[SearchQuery]): List of search queries to process

    Returns:
            List[dict]: List of search responses from Tavily API, one per query. Each response has format:
                {
                    'query': str, # The original search query
                    'follow_up_questions': None,      
                    'answer': None,
                    'images': list,
                    'results': [                     # List of search results
                        {
                            'title': str,            # Title of the webpage
                            'url': str,              # URL of the result
                            'content': str,          # Summary/snippet of content
                            'score': float,          # Relevance score
                            'raw_content': str|None  # Full page content if available
                        },
                        ...
                    ]
                }
    """
    
    search_tasks = []
    for query in search_queries:
            search_tasks.append(
                tavily_async_client.search(
                    query,
                    max_results=5,
                    include_raw_content=True,
                    topic="general"
                )
            )

    # Execute all searches concurrently
    search_docs = await asyncio.gather(*search_tasks)

    return search_docs

@traceable
def perplexity_search(search_queries):
    """Search the web using the Perplexity API.
    
    Args:
        search_queries (List[SearchQuery]): List of search queries to process
  
    Returns:
        List[dict]: List of search responses from Perplexity API, one per query. Each response has format:
            {
                'query': str,                    # The original search query
                'follow_up_questions': None,      
                'answer': None,
                'images': list,
                'results': [                     # List of search results
                    {
                        'title': str,            # Title of the search result
                        'url': str,              # URL of the result
                        'content': str,          # Summary/snippet of content
                        'score': float,          # Relevance score
                        'raw_content': str|None  # Full content or None for secondary citations
                    },
                    ...
                ]
            }
    """

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {os.getenv('PERPLEXITY_API_KEY')}"
    }
    
    search_docs = []
    for query in search_queries:

        payload = {
            "model": "sonar-pro",
            "messages": [
                {
                    "role": "system",
                    "content": "Search the web and provide factual information with sources."
                },
                {
                    "role": "user",
                    "content": query
                }
            ]
        }
        
        response = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers,
            json=payload
        )
        response.raise_for_status()  # Raise exception for bad status codes
        
        # Parse the response
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations", ["https://perplexity.ai"])
        
        # Create results list for this query
        results = []
        
        # First citation gets the full content
        results.append({
            "title": f"Perplexity Search, Source 1",
            "url": citations[0],
            "content": content,
            "raw_content": content,
            "score": 1.0  # Adding score to match Tavily format
        })
        
        # Add additional citations without duplicating content
        for i, citation in enumerate(citations[1:], start=2):
            results.append({
                "title": f"Perplexity Search, Source {i}",
                "url": citation,
                "content": "See primary source for full content",
                "raw_content": None,
                "score": 0.5  # Lower score for secondary sources
            })
        
        # Format response to match Tavily structure
        search_docs.append({
            "query": query,
            "follow_up_questions": None,
            "answer": None,
            "images": [],
            "results": results
        })
    
    return search_docs

@traceable
async def exa_search(search_queries, max_characters: Optional[int] = None, num_results=5, 
                     include_domains: Optional[List[str]] = None, 
                     exclude_domains: Optional[List[str]] = None,
                     subpages: Optional[int] = None):
    """Search the web using the Exa API.
    
    Args:
        search_queries (List[SearchQuery]): List of search queries to process
        max_characters (int, optional): Maximum number of characters to retrieve for each result's raw content.
                                       If None, the text parameter will be set to True instead of an object.
        num_results (int): Number of search results per query. Defaults to 5.
        include_domains (List[str], optional): List of domains to include in search results. 
            When specified, only results from these domains will be returned.
        exclude_domains (List[str], optional): List of domains to exclude from search results.
            Cannot be used together with include_domains.
        subpages (int, optional): Number of subpages to retrieve per result. If None, subpages are not retrieved.
        
    Returns:
        List[dict]: List of search responses from Exa API, one per query. Each response has format:
            {
                'query': str,                    # The original search query
                'follow_up_questions': None,      
                'answer': None,
                'images': list,
                'results': [                     # List of search results
                    {
                        'title': str,            # Title of the search result
                        'url': str,              # URL of the result
                        'content': str,          # Summary/snippet of content
                        'score': float,          # Relevance score
                        'raw_content': str|None  # Full content or None for secondary citations
                    },
                    ...
                ]
            }
    """
    # Check that include_domains and exclude_domains are not both specified
    if include_domains and exclude_domains:
        raise ValueError("Cannot specify both include_domains and exclude_domains")
    
    # Initialize Exa client (API key should be configured in your .env file)
    exa = Exa(api_key = f"{os.getenv('EXA_API_KEY')}")
    
    # Define the function to process a single query
    async def process_query(query):
        # Use run_in_executor to make the synchronous exa call in a non-blocking way
        loop = asyncio.get_event_loop()
        
        # Define the function for the executor with all parameters
        def exa_search_fn():
            # Build parameters dictionary
            kwargs = {
                # Set text to True if max_characters is None, otherwise use an object with max_characters
                "text": True if max_characters is None else {"max_characters": max_characters},
                "summary": True,  # This is an amazing feature by EXA. It provides an AI generated summary of the content based on the query
                "num_results": num_results
            }
            
            # Add optional parameters only if they are provided
            if subpages is not None:
                kwargs["subpages"] = subpages
                
            if include_domains:
                kwargs["include_domains"] = include_domains
            elif exclude_domains:
                kwargs["exclude_domains"] = exclude_domains
                
            return exa.search_and_contents(query, **kwargs)
        
        response = await loop.run_in_executor(None, exa_search_fn)
        
        # Format the response to match the expected output structure
        formatted_results = []
        seen_urls = set()  # Track URLs to avoid duplicates
        
        # Helper function to safely get value regardless of if item is dict or object
        def get_value(item, key, default=None):
            if isinstance(item, dict):
                return item.get(key, default)
            else:
                return getattr(item, key, default) if hasattr(item, key) else default
        
        # Access the results from the SearchResponse object
        results_list = get_value(response, 'results', [])
        
        # First process all main results
        for result in results_list:
            # Get the score with a default of 0.0 if it's None or not present
            score = get_value(result, 'score', 0.0)
            
            # Combine summary and text for content if both are available
            text_content = get_value(result, 'text', '')
            summary_content = get_value(result, 'summary', '')
            
            content = text_content
            if summary_content:
                if content:
                    content = f"{summary_content}\n\n{content}"
                else:
                    content = summary_content
            
            title = get_value(result, 'title', '')
            url = get_value(result, 'url', '')
            
            # Skip if we've seen this URL before (removes duplicate entries)
            if url in seen_urls:
                continue
                
            seen_urls.add(url)
            
            # Main result entry
            result_entry = {
                "title": title,
                "url": url,
                "content": content,
                "score": score,
                "raw_content": text_content
            }
            
            # Add the main result to the formatted results
            formatted_results.append(result_entry)
        
        # Now process subpages only if the subpages parameter was provided
        if subpages is not None:
            for result in results_list:
                subpages_list = get_value(result, 'subpages', [])
                for subpage in subpages_list:
                    # Get subpage score
                    subpage_score = get_value(subpage, 'score', 0.0)
                    
                    # Combine summary and text for subpage content
                    subpage_text = get_value(subpage, 'text', '')
                    subpage_summary = get_value(subpage, 'summary', '')
                    
                    subpage_content = subpage_text
                    if subpage_summary:
                        if subpage_content:
                            subpage_content = f"{subpage_summary}\n\n{subpage_content}"
                        else:
                            subpage_content = subpage_summary
                    
                    subpage_url = get_value(subpage, 'url', '')
                    
                    # Skip if we've seen this URL before
                    if subpage_url in seen_urls:
                        continue
                        
                    seen_urls.add(subpage_url)
                    
                    formatted_results.append({
                        "title": get_value(subpage, 'title', ''),
                        "url": subpage_url,
                        "content": subpage_content,
                        "score": subpage_score,
                        "raw_content": subpage_text
                    })
        
        # Collect images if available (only from main results to avoid duplication)
        images = []
        for result in results_list:
            image = get_value(result, 'image')
            if image and image not in images:  # Avoid duplicate images
                images.append(image)
                
        return {
            "query": query,
            "follow_up_questions": None,
            "answer": None,
            "images": images,
            "results": formatted_results
        }
    
    # Process all queries sequentially with delay to respect rate limit
    search_docs = []
    for i, query in enumerate(search_queries):
        try:
            # Add delay between requests (0.25s = 4 requests per second, well within the 5/s limit)
            if i > 0:  # Don't delay the first request
                await asyncio.sleep(0.25)
            
            result = await process_query(query)
            search_docs.append(result)
        except Exception as e:
            # Handle exceptions gracefully
            print(f"Error processing query '{query}': {str(e)}")
            # Add a placeholder result for failed queries to maintain index alignment
            search_docs.append({
                "query": query,
                "follow_up_questions": None,
                "answer": None,
                "images": [],
                "results": [],
                "error": str(e)
            })
            
            # Add additional delay if we hit a rate limit error
            if "429" in str(e):
                print("Rate limit exceeded. Adding additional delay...")
                await asyncio.sleep(1.0)  # Add a longer delay if we hit a rate limit
    
    return search_docs

@traceable
async def arxiv_search_async(search_queries, load_max_docs=5, get_full_documents=True, load_all_available_meta=True):
    """
    Performs concurrent searches on arXiv using the ArxivRetriever.

    Args:
        search_queries (List[str]): List of search queries or article IDs
        load_max_docs (int, optional): Maximum number of documents to return per query. Default is 5.
        get_full_documents (bool, optional): Whether to fetch full text of documents. Default is True.
        load_all_available_meta (bool, optional): Whether to load all available metadata. Default is True.

    Returns:
        List[dict]: List of search responses from arXiv, one per query. Each response has format:
            {
                'query': str,                    # The original search query
                'follow_up_questions': None,      
                'answer': None,
                'images': [],
                'results': [                     # List of search results
                    {
                        'title': str,            # Title of the paper
                        'url': str,              # URL (Entry ID) of the paper
                        'content': str,          # Formatted summary with metadata
                        'score': float,          # Relevance score (approximated)
                        'raw_content': str|None  # Full paper content if available
                    },
                    ...
                ]
            }
    """
    
    async def process_single_query(query):
        try:
            # Create retriever for each query
            retriever = ArxivRetriever(
                load_max_docs=load_max_docs,
                get_full_documents=get_full_documents,
                load_all_available_meta=load_all_available_meta
            )
            
            # Run the synchronous retriever in a thread pool
            loop = asyncio.get_event_loop()
            docs = await loop.run_in_executor(None, lambda: retriever.invoke(query))
            
            results = []
            # Assign decreasing scores based on the order
            base_score = 1.0
            score_decrement = 1.0 / (len(docs) + 1) if docs else 0
            
            for i, doc in enumerate(docs):
                # Extract metadata
                metadata = doc.metadata
                
                # Use entry_id as the URL (this is the actual arxiv link)
                url = metadata.get('entry_id', '')
                
                # Format content with all useful metadata
                content_parts = []

                # Primary information
                if 'Summary' in metadata:
                    content_parts.append(f"Summary: {metadata['Summary']}")

                if 'Authors' in metadata:
                    content_parts.append(f"Authors: {metadata['Authors']}")

                # Add publication information
                published = metadata.get('Published')
                published_str = published.isoformat() if hasattr(published, 'isoformat') else str(published) if published else ''
                if published_str:
                    content_parts.append(f"Published: {published_str}")

                # Add additional metadata if available
                if 'primary_category' in metadata:
                    content_parts.append(f"Primary Category: {metadata['primary_category']}")

                if 'categories' in metadata and metadata['categories']:
                    content_parts.append(f"Categories: {', '.join(metadata['categories'])}")

                if 'comment' in metadata and metadata['comment']:
                    content_parts.append(f"Comment: {metadata['comment']}")

                if 'journal_ref' in metadata and metadata['journal_ref']:
                    content_parts.append(f"Journal Reference: {metadata['journal_ref']}")

                if 'doi' in metadata and metadata['doi']:
                    content_parts.append(f"DOI: {metadata['doi']}")

                # Get PDF link if available in the links
                pdf_link = ""
                if 'links' in metadata and metadata['links']:
                    for link in metadata['links']:
                        if 'pdf' in link:
                            pdf_link = link
                            content_parts.append(f"PDF: {pdf_link}")
                            break

                # Join all content parts with newlines 
                content = "\n".join(content_parts)
                
                result = {
                    'title': metadata.get('Title', ''),
                    'url': url,  # Using entry_id as the URL
                    'content': content,
                    'score': base_score - (i * score_decrement),
                    'raw_content': doc.page_content if get_full_documents else None
                }
                results.append(result)
                
            return {
                'query': query,
                'follow_up_questions': None,
                'answer': None,
                'images': [],
                'results': results
            }
        except Exception as e:
            # Handle exceptions gracefully
            print(f"Error processing arXiv query '{query}': {str(e)}")
            return {
                'query': query,
                'follow_up_questions': None,
                'answer': None,
                'images': [],
                'results': [],
                'error': str(e)
            }
    
    # Process queries sequentially with delay to respect arXiv rate limit (1 request per 3 seconds)
    search_docs = []
    for i, query in enumerate(search_queries):
        try:
            # Add delay between requests (3 seconds per ArXiv's rate limit)
            if i > 0:  # Don't delay the first request
                await asyncio.sleep(3.0)
            
            result = await process_single_query(query)
            search_docs.append(result)
        except Exception as e:
            # Handle exceptions gracefully
            print(f"Error processing arXiv query '{query}': {str(e)}")
            search_docs.append({
                'query': query,
                'follow_up_questions': None,
                'answer': None,
                'images': [],
                'results': [],
                'error': str(e)
            })
            
            # Add additional delay if we hit a rate limit error
            if "429" in str(e) or "Too Many Requests" in str(e):
                print("ArXiv rate limit exceeded. Adding additional delay...")
                await asyncio.sleep(5.0)  # Add a longer delay if we hit a rate limit
    
    return search_docs

@traceable
async def pubmed_search_async(search_queries, top_k_results=5, email=None, api_key=None, doc_content_chars_max=4000):
    """
    Performs concurrent searches on PubMed using the PubMedAPIWrapper.

    Args:
        search_queries (List[str]): List of search queries
        top_k_results (int, optional): Maximum number of documents to return per query. Default is 5.
        email (str, optional): Email address for PubMed API. Required by NCBI.
        api_key (str, optional): API key for PubMed API for higher rate limits.
        doc_content_chars_max (int, optional): Maximum characters for document content. Default is 4000.

    Returns:
        List[dict]: List of search responses from PubMed, one per query. Each response has format:
            {
                'query': str,                    # The original search query
                'follow_up_questions': None,      
                'answer': None,
                'images': [],
                'results': [                     # List of search results
                    {
                        'title': str,            # Title of the paper
                        'url': str,              # URL to the paper on PubMed
                        'content': str,          # Formatted summary with metadata
                        'score': float,          # Relevance score (approximated)
                        'raw_content': str       # Full abstract content
                    },
                    ...
                ]
            }
    """
    
    async def process_single_query(query):
        try:
            # print(f"Processing PubMed query: '{query}'")
            
            # Create PubMed wrapper for the query
            wrapper = PubMedAPIWrapper(
                top_k_results=top_k_results,
                doc_content_chars_max=doc_content_chars_max,
                email=email if email else "your_email@example.com",
                api_key=api_key if api_key else ""
            )
            
            # Run the synchronous wrapper in a thread pool
            loop = asyncio.get_event_loop()
            
            # Use wrapper.lazy_load instead of load to get better visibility
            docs = await loop.run_in_executor(None, lambda: list(wrapper.lazy_load(query)))
            
            print(f"Query '{query}' returned {len(docs)} results")
            
            results = []
            # Assign decreasing scores based on the order
            base_score = 1.0
            score_decrement = 1.0 / (len(docs) + 1) if docs else 0
            
            for i, doc in enumerate(docs):
                # Format content with metadata
                content_parts = []
                
                if doc.get('Published'):
                    content_parts.append(f"Published: {doc['Published']}")
                
                if doc.get('Copyright Information'):
                    content_parts.append(f"Copyright Information: {doc['Copyright Information']}")
                
                if doc.get('Summary'):
                    content_parts.append(f"Summary: {doc['Summary']}")
                
                # Generate PubMed URL from the article UID
                uid = doc.get('uid', '')
                url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/" if uid else ""
                
                # Join all content parts with newlines
                content = "\n".join(content_parts)
                
                result = {
                    'title': doc.get('Title', ''),
                    'url': url,
                    'content': content,
                    'score': base_score - (i * score_decrement),
                    'raw_content': doc.get('Summary', '')
                }
                results.append(result)
            
            return {
                'query': query,
                'follow_up_questions': None,
                'answer': None,
                'images': [],
                'results': results
            }
        except Exception as e:
            # Handle exceptions with more detailed information
            error_msg = f"Error processing PubMed query '{query}': {str(e)}"
            print(error_msg)
            import traceback
            print(traceback.format_exc())  # Print full traceback for debugging
            
            return {
                'query': query,
                'follow_up_questions': None,
                'answer': None,
                'images': [],
                'results': [],
                'error': str(e)
            }
    
    # Process all queries with a reasonable delay between them
    search_docs = []
    
    # Start with a small delay that increases if we encounter rate limiting
    delay = 1.0  # Start with a more conservative delay
    
    for i, query in enumerate(search_queries):
        try:
            # Add delay between requests
            if i > 0:  # Don't delay the first request
                # print(f"Waiting {delay} seconds before next query...")
                await asyncio.sleep(delay)
            
            result = await process_single_query(query)
            search_docs.append(result)
            
            # If query was successful with results, we can slightly reduce delay (but not below minimum)
            if result.get('results') and len(result['results']) > 0:
                delay = max(0.5, delay * 0.9)  # Don't go below 0.5 seconds
            
        except Exception as e:
            # Handle exceptions gracefully
            error_msg = f"Error in main loop processing PubMed query '{query}': {str(e)}"
            print(error_msg)
            
            search_docs.append({
                'query': query,
                'follow_up_questions': None,
                'answer': None,
                'images': [],
                'results': [],
                'error': str(e)
            })
            
            # If we hit an exception, increase delay for next query
            delay = min(5.0, delay * 1.5)  # Don't exceed 5 seconds
    
    return search_docs

@traceable
def list_vapi_files() -> List[Dict[str, Any]]:
    """List all files in the Vapi account.
    
    Returns:
        List[Dict[str, Any]]: List of file objects
    """
    url = "https://api.vapi.ai/file"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def delete_vapi_file(file_id: str) -> Dict[str, Any]:
    """Delete a file from Vapi.
    
    Args:
        file_id: ID of the file to delete
        
    Returns:
        Dict[str, Any]: Response from the API
    """
    url = f"https://api.vapi.ai/file/{file_id}"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def list_vapi_tools() -> List[Dict[str, Any]]:
    """List all tools in the Vapi account.
    
    Returns:
        List[Dict[str, Any]]: List of tool objects
    """
    url = "https://api.vapi.ai/tool"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def delete_vapi_tool(tool_id: str) -> Dict[str, Any]:
    """Delete a tool from Vapi.
    
    Args:
        tool_id: ID of the tool to delete
        
    Returns:
        Dict[str, Any]: Response from the API
    """
    url = f"https://api.vapi.ai/tool/{tool_id}"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def list_vapi_assistants() -> List[Dict[str, Any]]:
    """List all assistants in the Vapi account.
    
    Returns:
        List[Dict[str, Any]]: List of assistant objects
    """
    url = "https://api.vapi.ai/assistant"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def delete_vapi_assistant(assistant_id: str) -> Dict[str, Any]:
    """Delete an assistant from Vapi.
    
    Args:
        assistant_id: ID of the assistant to delete
        
    Returns:
        Dict[str, Any]: Response from the API
    """
    url = f"https://api.vapi.ai/assistant/{assistant_id}"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def cleanup_vapi_resources_by_name(file_name: str = None, tool_name: str = None, assistant_name: str = None):
    """Clean up Vapi resources by name.
    
    Args:
        file_name: Name of the file to delete (if exists)
        tool_name: Name of the tool to delete (if exists)
        assistant_name: Name of the assistant to delete (if exists)
    """
    # Clean up files with matching name
    if file_name:
        files = list_vapi_files()
        for file in files:
            if file.get("name") == file_name or file.get("originalName") == file_name:
                print(f"Deleting file: {file['name']} (ID: {file['id']})")
                delete_vapi_file(file["id"])
    
    # Clean up tools with matching name
    if tool_name:
        tools = list_vapi_tools()
        for tool in tools:
            if tool.get("function", {}).get("name") == tool_name:
                print(f"Deleting tool: {tool['function']['name']} (ID: {tool['id']})")
                delete_vapi_tool(tool["id"])
    
    # Clean up assistants with matching name
    if assistant_name:
        assistants = list_vapi_assistants()
        for assistant in assistants:
            if assistant.get("name") == assistant_name:
                print(f"Deleting assistant: {assistant['name']} (ID: {assistant['id']})")
                delete_vapi_assistant(assistant["id"])

@traceable
def upload_file_to_vapi(file_path: str, file_name: str = None) -> str:
    """Upload a file to Vapi, deleting any existing file with the same name.
    
    Args:
        file_path: Path to the file to upload
        file_name: Optional name for the file (if None, uses the original filename)
        
    Returns:
        str: File ID
    """
    import os
    import pathlib
    
    # If no file_name is provided, use the original filename
    if file_name is None:
        file_name = pathlib.Path(file_path).name
    
    # Clean up any existing file with the same name
    cleanup_vapi_resources_by_name(file_name=file_name)
    
    url = "https://api.vapi.ai/file"
    headers = {
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    # Upload the file directly from the path
    with open(file_path, 'rb') as f:
        files = {'file': (file_name, f, 'application/pdf')}
        response = requests.post(url, headers=headers, files=files)
        response.raise_for_status()
        return response.json()["id"]

@traceable
def create_query_tool(file_id: str, name: str = "legislation-query-tool") -> str:
    """Create a query tool that references a file, deleting any existing tool with the same name.
    
    Args:
        file_id: ID of the file to reference
        name: Name for the query tool
        
    Returns:
        str: Tool ID
    """
    import os
    
    # Clean up any existing tool with the same name
    cleanup_vapi_resources_by_name(tool_name=name)
    
    url = "https://api.vapi.ai/tool"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    payload = {
        "type": "query",
        "function": {
            "name": name
        },
        "knowledgeBases": [
            {
                "provider": "google",
                "name": "legislation-knowledge-base",
                "description": "Use this knowledge base when the user asks about specific details of the legislation, its provisions, or its implications.",
                "fileIds": [file_id]
            }
        ]
    }
    
    # Pretty print the payload for easier debugging
    import json
    print("Query Tool Creation Payload:")
    print(json.dumps(payload, indent=4))
    
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()["id"]

@traceable
def create_vapi_assistant(name: str, system_prompt: str, first_message: str = None, 
                         end_call_message: str = None, analysis_plan: dict = None,
                         tool_ids: List[str] = None) -> str:
    """Create a new Vapi assistant, deleting any existing assistant with the same name.
    
    Args:
        name: Name for the assistant
        system_prompt: System prompt for the assistant
        first_message: First message the assistant will say when the call connects
        end_call_message: Message the assistant will say before ending the call
        analysis_plan: Configuration for call analysis and outcome reporting
        tool_ids: IDs of the tools to attach to the assistant
        
    Returns:
        str: Assistant ID
    """
    import os
    
    # Clean up any existing assistant with the same name
    cleanup_vapi_resources_by_name(assistant_name=name)
    
    url = "https://api.vapi.ai/assistant"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    payload = {
        "name": name,
        "model": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                }
            ],
            "emotionRecognitionEnabled": True,
            "maxTokens": 100,
            "knowledgeBaseId": os.getenv("VAPI_KNOWLEDGE_BASE_ID")
        },
        "voice": {
            "provider": "playht",
            "voiceId": "jennifer",
            "model": "PlayDialog"
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "en"
        },
        "messagePlan": {
            "idleMessages": ["Hello?", "Are you still there?", "Can you hear me?"]
        },
        # TODO: add?
        # "startSpeakingPlan": {
        #     "smartEndpointingEnabled": True,
        # },
    }
    
    # Add optional parameters if provided
    if first_message:
        payload["firstMessage"] = first_message
        payload["firstMessageMode"] = "assistant-speaks-first"
    
    if end_call_message:
        payload["endCallMessage"] = end_call_message
    
    if analysis_plan:
        payload["analysisPlan"] = analysis_plan
    
    # Configure proper stop speaking behavior
    payload["stopSpeakingPlan"] = {
        "acknowledgementPhrases": [
            "i understand",
            "i see",
            "i got it",
            "i hear you",
            "im listening",
            "im with you",
            "right",
            "okay",
            "ok",
            "sure",
            "alright",
            "got it",
            "understood",
            "yeah",
            "yes",
            "uh-huh",
            "mm-hmm",
            "gotcha",
            "mhmm",
            "ah",
            "yeah okay",
            "yeah sure"
            ],
        "interruptionPhrases": [
            "stop",
            "shut",
            "up",
            "enough",
            "quiet",
            "silence",
            "but",
            "dont",
            "not",
            "no",
            "hold",
            "wait",
            "cut",
            "pause",
            "nope",
            "nah",
            "nevermind",
            "never",
            "bad",
            "actually"
        ]
    }
    
    # Add tool ID if provided
    if tool_ids:
        if "model" not in payload:
            payload["model"] = {}
        
        payload["model"]["toolIds"] = tool_ids
        payload["model"]["tools"] = [
            {
                "type": "endCall"
            },
            {
                "type": "voicemail"
            },
            {
                "type": "transferCall"
            }
        ]
    
    # Pretty print the payload for easier debugging
    import json
    print("Assistant Creation Payload:")
    print(json.dumps(payload, indent=2))
    
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()["id"]

@traceable
def make_vapi_call(assistant_id: str, phone_number_id: str, customer_number: str) -> Dict[str, Any]:
    """Initiate an outbound call using Vapi.
    
    Args:
        assistant_id: ID of the Vapi assistant
        phone_number_id: ID of the phone number to call from
        customer_number: Phone number to call
        
    Returns:
        Dict: Call information including call_id
    """
    url = "https://api.vapi.ai/call"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    payload = {
        "assistantId": assistant_id,
        "phoneNumberId": phone_number_id,
        "customer": {
            "number": customer_number,
            "name": "Dirk"
        }
    }
    
    # Pretty print the payload for easier debugging
    import json
    print("Call Creation Payload:")
    print(json.dumps(payload, indent=2))
    
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()

@traceable
def get_vapi_call_status(call_id: str) -> Dict[str, Any]:
    """Get the status of a Vapi call.
    
    Args:
        call_id: ID of the call
        
    Returns:
        Dict: Call status information
    """
    url = f"https://api.vapi.ai/call/{call_id}"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {os.getenv('VAPI_API_KEY')}"
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

# Add caching utilities based on the notebook example
def setup_embedding_cache(embedding_model, namespace: str):
    """Set up cache-backed embeddings.
    
    Args:
        embedding_model: Base embedding model
        namespace: Namespace for the cache
        
    Returns:
        CacheBackedEmbeddings: Cached embedding model
    """
    import hashlib

    from langchain.embeddings import CacheBackedEmbeddings
    from langchain.storage import LocalFileStore
    
    # Create a safe namespace by hashing the model identifier
    safe_namespace = hashlib.md5(namespace.encode()).hexdigest()
    
    # Set up local file store for cache
    store = LocalFileStore("./cache/")
    
    # Create cache-backed embeddings
    return CacheBackedEmbeddings.from_bytes_store(
        embedding_model, store, namespace=safe_namespace, batch_size=32
    )

def setup_persistent_vectorstore(embedding_model, collection_name: str, path: str = "./vector_store"):
    """Set up a persistent vector store.
    
    Args:
        embedding_model: Embedding model to use
        collection_name: Name for the collection
        path: Path to store the vector database
        
    Returns:
        VectorStore: Configured vector store
    """
    from langchain_qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import Distance, VectorParams
    
    # Create Qdrant client with persistent storage
    client = QdrantClient(path=path)
    
    # Check if collection exists, create if not
    collections = client.get_collections().collections
    collection_names = [collection.name for collection in collections]
    
    if collection_name not in collection_names:
        # Create new collection
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )
    
    # Create vector store
    return QdrantVectorStore(
        client=client,
        collection_name=collection_name,
        embedding=embedding_model
    )

def setup_llm_cache():
    """Set up LLM response caching."""
    from langchain_core.caches import SQLiteCache
    from langchain_core.globals import set_llm_cache
    
    # Use SQLite for persistent caching
    set_llm_cache(SQLiteCache(database_path="./cache/llm_cache.db"))

@traceable
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from a PDF file.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        str: Extracted text from the PDF
    """
    from langchain_community.document_loaders import PyMuPDFLoader
    
    loader = PyMuPDFLoader(pdf_path)
    documents = loader.load()
    
    # Combine all document pages into a single text
    text = "\n\n".join([doc.page_content for doc in documents])
    
    # Log the text length for tracing
    print(f"Extracted {len(text)} characters from {pdf_path}")
    
    return text

@traceable
def upload_report_to_trieve(file_path, report_topic):
    """Upload a report file to Trieve for vectorization and retrieval.
    
    Args:
        file_path: Path to the report file
        report_topic: Topic of the report
    """
    import os
    import base64
    import requests
    from datetime import datetime

    # Check if Trieve API key and dataset ID are available
    api_key = os.getenv("TRIEVE_API_KEY")
    dataset_id = os.getenv("TRIEVE_DATASET_ID")
    
    if not api_key or not dataset_id:
        print("Trieve API key or dataset ID not found. Skipping upload to Trieve.")
        return

    # Read the file
    with open(file_path, "rb") as file:
        file_content = file.read()
    
    # Encode file to base64
    base64_file = base64.b64encode(file_content).decode("utf-8")
    
    # Get filename from path
    file_name = os.path.basename(file_path)
    
    # Configure Trieve API request
    url = "https://api.trieve.ai/api/file"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "TR-Dataset": dataset_id,
    }
    
    # Prepare request body - using settings optimized for our use case:
    # - Small target splits per chunk (8-12) for more precise retrieval
    # - Rebalance chunks to ensure even distribution of content
    # - Include relevant metadata for filtering
    payload = {
        "base64_file": base64_file,
        "file_name": file_name,
        "create_chunks": True,
        "description": report_topic,
        "target_splits_per_chunk": 10,  # Balanced size for political research reports
        "rebalance_chunks": True,       # Ensure even chunk distribution
        # "split_delimiters": [".", "!", "?", "\n\n"],  # Split on sentences and paragraphs
        "metadata": {
            "report_topic": report_topic,
            "created_at": datetime.now().isoformat(),
            "content_type": "political_research"
        },
        "time_stamp": datetime.now().isoformat()
    }
    print(f"Trieve payload: {payload}")
    
    # Make the API request
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Successfully uploaded {file_name} to Trieve: {response.json()}")
        return response.json()
    except Exception as e:
        print(f"Error uploading to Trieve: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"Response content: {e.response.content}")
        return None